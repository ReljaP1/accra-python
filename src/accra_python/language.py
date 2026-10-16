from accra_language import Config, EnvironmentManager, Language, LanguageSpec

from .uv import Uv


class Python3(Language):
    def __init__(self, config: Config, env_manager: EnvironmentManager | None = None):
        spec = LanguageSpec(
            name="python3",
            version="3",
            config=config,
            supported_env_managers={Uv(config)},
        )
        super().__init__(spec, env_manager=env_manager)
