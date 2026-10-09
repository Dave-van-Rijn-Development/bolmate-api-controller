import os
from configparser import ConfigParser, SectionProxy, MissingSectionHeaderError
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Settings(BaseModel):
    model_config = ConfigDict(extra='allow')

    # Encryption
    bolmate_encrypt_secret: str
    bolmate_encryption_value: str
    bolmate_encryption_iterations: int

    # Bol OAuth
    bol_oauth_client_id: str
    bol_oauth_client_secret: str
    bol_oauth_insights_client_id: str
    bol_oauth_insights_client_secret: str

    # Bolmate database
    bolmate_database_user: str
    bolmate_database_password: str
    bolmate_database_name: str
    bolmate_database_port: int
    bolmate_database_host: str


_config_override: dict[str, Any] | None = None
_file_path_override: str | Path | None = None
_config_section_override: str | None = None


class ConfigReader:
    # Sections to try to parse
    DEFAULT_SECTIONS = ['bolmate_api_controller', 'main', 'DEFAULT']

    def __init__(self, config_file_path: str | Path | None = None, section: str | None = None):
        self._explicit_path = config_file_path is not None
        self._config_file_path = Path(config_file_path) if config_file_path is not None else Path.cwd() / 'config.ini'
        self._section = section

    def build_settings(self) -> Settings:
        config = self._read_config()
        if _config_override is None:
            # Include env var overrides, only when reading from config file
            env = {k.lower(): v for k, v in os.environ.items()}
            config = {**config, **{f: env[f] for f in Settings.model_fields if f in env}}
        return Settings.model_validate(config)

    def _read_config(self) -> dict[str, Any]:
        if _config_override is not None:
            return _config_override
        if not self._config_file_path.exists():
            if self._explicit_path:
                raise FileNotFoundError(f'Specified config file does not exist: {self._config_file_path}')
            return {}
        parser = ConfigParser(interpolation=None)
        try:
            parser.read(self._config_file_path)
        except MissingSectionHeaderError:
            # No section header in the file, could be a dotenv file. Append our own section header to still be able to
            # read it as ini file
            with self._config_file_path.open() as f:
                parser.read_string('[DEFAULT]\n' + f.read())
        section_proxy, attempted_sections = self._find_section(parser=parser)
        if not section_proxy:
            raise ValueError(
                f'Could not find section in specified config file, attempted sections: {", ".join(attempted_sections)}')
        section = dict(section_proxy)
        return section

    def _find_section(self, *, parser: ConfigParser) -> tuple[SectionProxy | None, list[str]]:
        if self._section is not None:
            if not _config_has_section(parser=parser, section=self._section):
                raise ValueError(f'Section {self._section} not found in specified config file')
            return parser[self._section], [self._section]

        attempted_sections: list[str] = []
        for section in self.DEFAULT_SECTIONS:
            attempted_sections.append(section)
            if _config_has_section(parser=parser, section=section):
                return parser[section], attempted_sections
        return None, attempted_sections


def _config_has_section(*, parser: ConfigParser, section: str) -> bool:
    if section == parser.default_section:
        return bool(parser.defaults())
    return parser.has_section(section)


_NOT_SET = 'not_set'
type _NotSet = Literal['not_set']


@lru_cache
def get_settings(*, ini_file: str | Path | None = None, config_section: str | None = None) -> Settings:
    if ini_file is None:
        ini_file = _file_path_override
    if config_section is None:
        config_section = _config_section_override
    return ConfigReader(config_file_path=ini_file, section=config_section).build_settings()


def override_settings_variables(*, config_file_path: str | Path | None | _NotSet = _NOT_SET,
                                config_section: str | None | _NotSet = _NOT_SET):
    global _file_path_override, _config_section_override
    if config_file_path != _NOT_SET:
        _file_path_override = config_file_path
    if config_section != _NOT_SET:
        _config_section_override = config_section
    # Clear cache to allow rebuilding the settings
    get_settings.cache_clear()


def clear_override_settings_variables():
    override_settings_variables(config_file_path=None, config_section=None)


def override_config(*, config: dict[str, Any]):
    global _config_override
    # Immediately parse the config to catch errors early
    Settings.model_validate(config)
    _config_override = config
    # Clear cache to allow rebuilding the settings
    get_settings.cache_clear()


def clear_config_override():
    global _config_override
    _config_override = None
    # Clear cache to allow rebuilding the settings
    get_settings.cache_clear()
