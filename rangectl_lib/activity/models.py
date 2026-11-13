from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from rangectl_lib.security import secure_write_text

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


class ActivityConfigError(Exception):
    pass


def _as_mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ActivityConfigError(f"{label} must be a mapping")
    return value


def _as_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ActivityConfigError(f"{label} must be a list")
    return value


@dataclass(slots=True)
class WorkHours:
    start: str = "09:00"
    end: str = "17:00"
    timezone: str = "local"
    weekdays: list[str] = field(default_factory=lambda: ["monday", "tuesday", "wednesday", "thursday", "friday"])

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> WorkHours:
        data = _as_mapping(data, "work_hours")
        return cls(
            start=str(data.get("start", "09:00")),
            end=str(data.get("end", "17:00")),
            timezone=str(data.get("timezone", "local")),
            weekdays=_normalize_weekdays(data.get("weekdays", data.get("days"))),
        )


@dataclass(slots=True)
class EmailProfile:
    address: str = ""
    backend: str = "external"
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EmailProfile:
        data = _as_mapping(data, "email")
        return cls(
            address=str(data.get("address", "")),
            backend=str(data.get("backend", data.get("type", "external"))),
            settings=dict(_as_mapping(data.get("settings"), "email.settings")),
        )


@dataclass(slots=True)
class Persona:
    id: str
    username: str
    host: str
    role: str = "office"
    display_name: str = ""
    department: str = ""
    interests: list[str] = field(default_factory=list)
    traits: dict[str, Any] = field(default_factory=dict)
    work_hours: WorkHours = field(default_factory=WorkHours)
    email: EmailProfile = field(default_factory=EmailProfile)

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_work_hours: WorkHours | None = None) -> Persona:
        data = _as_mapping(data, "persona")
        missing = [key for key in ("id", "username", "host") if not data.get(key)]
        if missing:
            raise ActivityConfigError(f"persona missing required fields: {', '.join(missing)}")
        work_hours = WorkHours.from_dict(data.get("work_hours")) if data.get("work_hours") is not None else (default_work_hours or WorkHours())
        return cls(
            id=str(data["id"]),
            username=str(data["username"]),
            host=str(data["host"]),
            role=str(data.get("role", "office")),
            display_name=str(data.get("display_name", data.get("name", ""))),
            department=str(data.get("department", "")),
            interests=[str(item) for item in _as_list(data.get("interests"), "persona.interests")],
            traits=dict(_as_mapping(data.get("traits"), "persona.traits")),
            work_hours=work_hours,
            email=EmailProfile.from_dict(data.get("email")),
        )


@dataclass(slots=True)
class ActionSpec:
    type: str
    args: dict[str, Any] = field(default_factory=dict)
    probability: int = 100
    delay_before: int = 0
    delay_after: int = 0
    delay_before_jitter: int = 0
    delay_after_jitter: int = 0
    trackable_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionSpec:
        data = _as_mapping(data, "action")
        action_type = data.get("type")
        if not action_type:
            raise ActivityConfigError("action.type is required")
        probability = int(data.get("probability", 100))
        if probability < 0 or probability > 100:
            raise ActivityConfigError(f"action {action_type!r} probability must be 0-100")
        args = dict(_as_mapping(data.get("args"), f"action {action_type}.args"))
        reserved = {
            "type",
            "args",
            "probability",
            "delay_before",
            "delay_after",
            "delay_before_jitter",
            "delay_after_jitter",
            "trackable_id",
        }
        for key, value in data.items():
            if key not in reserved:
                args.setdefault(key, value)
        return cls(
            type=str(action_type),
            args=args,
            probability=probability,
            delay_before=int(data.get("delay_before", 0)),
            delay_after=int(data.get("delay_after", 0)),
            delay_before_jitter=int(data.get("delay_before_jitter", 0)),
            delay_after_jitter=int(data.get("delay_after_jitter", 0)),
            trackable_id=str(data["trackable_id"]) if data.get("trackable_id") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "args": self.args,
            "probability": self.probability,
            "delay_before": self.delay_before,
            "delay_after": self.delay_after,
            "delay_before_jitter": self.delay_before_jitter,
            "delay_after_jitter": self.delay_after_jitter,
        }
        if self.trackable_id:
            data["trackable_id"] = self.trackable_id
        return data


@dataclass(slots=True)
class RoleProfile:
    name: str
    actions: list[ActionSpec] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> RoleProfile:
        data = _as_mapping(data, f"profiles.{name}")
        actions = [ActionSpec.from_dict(item) for item in _as_list(data.get("actions"), f"profiles.{name}.actions")]
        settings = {key: value for key, value in data.items() if key != "actions"}
        return cls(name=name, actions=actions, settings=settings)


@dataclass(slots=True)
class AiSettings:
    provider: str = "static"
    cache_dir: str = "activity/content-cache"
    endpoint_env: str = "OPENAI_BASE_URL"
    api_key_env: str = "OPENAI_API_KEY"
    model_env: str = "OPENAI_MODEL"
    default_model: str = "gpt-4.1-mini"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AiSettings:
        data = _as_mapping(data, "activity.ai")
        return cls(
            provider=str(data.get("provider", "static")),
            cache_dir=str(data.get("cache_dir", "activity/content-cache")),
            endpoint_env=str(data.get("endpoint_env", "OPENAI_BASE_URL")),
            api_key_env=str(data.get("api_key_env", "OPENAI_API_KEY")),
            model_env=str(data.get("model_env", "OPENAI_MODEL")),
            default_model=str(data.get("default_model", "gpt-4.1-mini")),
        )


@dataclass(slots=True)
class RangeActivity:
    enabled: bool = False
    personas: list[Persona] = field(default_factory=list)
    profiles: dict[str, RoleProfile] = field(default_factory=dict)
    ai: AiSettings = field(default_factory=AiSettings)
    defaults: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_range(cls, range_data: dict[str, Any]) -> RangeActivity:
        section = _as_mapping(range_data.get("activity"), "activity")
        return cls.from_dict(section, range_data=range_data)

    @classmethod
    def from_file(cls, path: Path) -> RangeActivity:
        data = _load_structured_file(path)
        if not isinstance(data, dict):
            raise ActivityConfigError(f"activity file must parse to a mapping: {path}")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any], range_data: dict[str, Any] | None = None) -> RangeActivity:
        enabled = bool(data.get("enabled", False))
        profiles = {
            str(name): RoleProfile.from_dict(str(name), value)
            for name, value in _as_mapping(data.get("profiles"), "activity.profiles").items()
        }
        defaults = dict(_as_mapping(data.get("defaults"), "activity.defaults"))
        default_work_hours = WorkHours.from_dict(defaults.get("work_hours")) if defaults.get("work_hours") is not None else None
        personas = [
            Persona.from_dict(item, default_work_hours=default_work_hours)
            for item in _as_list(data.get("personas"), "activity.personas")
        ]
        if not personas and range_data is not None and data.get("autodiscover", True):
            personas = discover_personas(range_data, defaults)
        if not profiles:
            profiles = default_profiles()
        return cls(
            enabled=enabled,
            personas=personas,
            profiles=profiles,
            ai=AiSettings.from_dict(data.get("ai")),
            defaults=defaults,
        )


@dataclass(slots=True)
class AgentConfig:
    range_name: str
    host: str
    persona: Persona
    actions: list[ActionSpec]
    ai: AiSettings
    state_dir: str
    loop: bool = True
    max_actions_per_cycle: int = 3
    cycle_delay: int = 300
    cycle_jitter: int = 0
    action_delay_jitter: int = 0
    enforce_work_hours: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "range_name": self.range_name,
            "host": self.host,
            "state_dir": self.state_dir,
            "loop": self.loop,
            "max_actions_per_cycle": self.max_actions_per_cycle,
            "cycle_delay": self.cycle_delay,
            "cycle_jitter": self.cycle_jitter,
            "action_delay_jitter": self.action_delay_jitter,
            "enforce_work_hours": self.enforce_work_hours,
            "ai": {
                "provider": self.ai.provider,
                "cache_dir": self.ai.cache_dir,
                "endpoint_env": self.ai.endpoint_env,
                "api_key_env": self.ai.api_key_env,
                "model_env": self.ai.model_env,
                "default_model": self.ai.default_model,
            },
            "persona": {
                "id": self.persona.id,
                "username": self.persona.username,
                "host": self.persona.host,
                "role": self.persona.role,
                "display_name": self.persona.display_name,
                "department": self.persona.department,
                "interests": self.persona.interests,
                "traits": self.persona.traits,
                "work_hours": {
                    "start": self.persona.work_hours.start,
                    "end": self.persona.work_hours.end,
                    "timezone": self.persona.work_hours.timezone,
                    "weekdays": self.persona.work_hours.weekdays,
                },
                "email": {
                    "address": self.persona.email.address,
                    "backend": self.persona.email.backend,
                    "settings": self.persona.email.settings,
                },
            },
            "actions": [action.to_dict() for action in self.actions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentConfig:
        data = _as_mapping(data, "agent config")
        persona = Persona.from_dict(_as_mapping(data.get("persona"), "persona"))
        ai = AiSettings.from_dict(data.get("ai"))
        actions = [ActionSpec.from_dict(item) for item in _as_list(data.get("actions"), "actions")]
        return cls(
            range_name=str(data.get("range_name", "unknown")),
            host=str(data.get("host", persona.host)),
            persona=persona,
            actions=actions,
            ai=ai,
            state_dir=str(data.get("state_dir", ".")),
            loop=bool(data.get("loop", True)),
            max_actions_per_cycle=int(data.get("max_actions_per_cycle", 3)),
            cycle_delay=int(data.get("cycle_delay", 300)),
            cycle_jitter=int(data.get("cycle_jitter", 0)),
            action_delay_jitter=int(data.get("action_delay_jitter", 0)),
            enforce_work_hours=bool(data.get("enforce_work_hours", False)),
        )

    @classmethod
    def from_file(cls, path: Path) -> AgentConfig:
        data = _load_structured_file(path)
        if not isinstance(data, dict):
            raise ActivityConfigError(f"agent config must parse to a mapping: {path}")
        return cls.from_dict(data)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() == ".json" or yaml is None:
            secure_write_text(path, json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")
        else:
            secure_write_text(path, yaml.safe_dump(self.to_dict(), sort_keys=False))


def _load_structured_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    if yaml is None:
        return json.loads(text)
    return yaml.safe_load(text)


def _normalize_weekdays(value: Any) -> list[str]:
    if value is None:
        return ["monday", "tuesday", "wednesday", "thursday", "friday"]
    if isinstance(value, str):
        items: list[Any] = [item.strip() for item in value.split(",") if item.strip()]
    else:
        items = _as_list(value, "work_hours.weekdays")

    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    aliases = {name: name for name in names}
    aliases.update({name[:3]: name for name in names})
    aliases.update({str(index): name for index, name in enumerate(names)})
    aliases.update({str(index + 1): name for index, name in enumerate(names)})
    aliases["weekday"] = "weekday"
    aliases["weekdays"] = "weekday"
    aliases["all"] = "all"
    aliases["daily"] = "all"

    normalized: list[str] = []
    for item in items:
        key = str(item).strip().lower()
        if not key:
            continue
        name = aliases.get(key)
        if name is None:
            raise ActivityConfigError(f"unsupported work_hours weekday: {item}")
        if name == "weekday":
            expanded = names[:5]
        elif name == "all":
            expanded = names
        else:
            expanded = [name]
        for day in expanded:
            if day not in normalized:
                normalized.append(day)
    return normalized


def discover_personas(range_data: dict[str, Any], defaults: dict[str, Any]) -> list[Persona]:
    domain = str((range_data.get("ansible") or {}).get("vars", {}).get("domain_name", "local"))
    discovered: list[Persona] = []
    user_map = defaults.get("user_map") or {}
    role_map = defaults.get("role_map") or {}
    for host in range_data.get("hosts") or []:
        if not isinstance(host, dict):
            continue
        groups = set(host.get("groups") or ((host.get("ansible") or {}).get("groups") or []))
        os_name = str(host.get("os", ""))
        if "workstation" not in groups and "macos" not in groups and not os_name.startswith("windows"):
            continue
        host_name = str(host.get("name") or host.get("hostname"))
        username = str(user_map.get(host_name) or _username_from_host(host_name))
        role = str(role_map.get(host_name) or _role_from_host(host_name))
        discovered.append(
            Persona(
                id=username.replace(".", "-"),
                username=username,
                host=host_name,
                role=role,
                display_name=username.replace(".", " ").title(),
                email=EmailProfile(
                    address=f"{username}@{domain}.local" if not domain.endswith(".local") else f"{username}@{domain}",
                    backend="outlook" if os_name.startswith("windows") or "windows" in groups else "external",
                ),
            )
        )
    return discovered


def default_profiles() -> dict[str, RoleProfile]:
    web_urls = _default_web_urls()
    common = [
        ActionSpec(type="browser.visit", args={"urls": web_urls, "reuse_tab": True, "max_tabs": 4}, delay_after=30),
        ActionSpec(
            type="application.open_close",
            args={"close_probability": 70, "min_dwell_seconds": 8, "max_dwell_seconds": 45},
            probability=35,
            delay_after=5,
        ),
        ActionSpec(type="office.word", args={"directory": "~/Documents", "topic": "work notes"}, probability=60),
        ActionSpec(type="email.process", args={"mode": "routine"}, probability=50, delay_after=20),
        ActionSpec(type="email.phishing", args={"mode": "phishing"}, probability=20, delay_after=10),
    ]
    return {
        "office": RoleProfile(name="office", actions=list(common)),
        "sales": RoleProfile(
            name="sales",
            actions=common + [
                ActionSpec(type="file.create_csv", args={"directory": "~/Documents", "topic": "sales pipeline"}, probability=70),
                ActionSpec(type="office.powerpoint", args={"directory": "~/Documents", "topic": "customer update"}, probability=25),
            ],
        ),
        "it": RoleProfile(
            name="it",
            actions=common + [
                ActionSpec(type="shell.run", args={"commands": ["ipconfig /all", "whoami", "net use"], "shell": False}, probability=40),
                ActionSpec(type="office.excel", args={"directory": "~/Documents", "topic": "endpoint inventory"}, probability=35),
            ],
        ),
        "hr": RoleProfile(
            name="hr",
            actions=common + [
                ActionSpec(type="office.word", args={"directory": "~/Documents", "topic": "candidate feedback"}, probability=70),
            ],
        ),
    }


def _default_web_urls() -> list[str]:
    urls = [_normalize_web_url(item) for item in _load_humansim_websites()]
    urls = [item for item in urls if item]
    if urls:
        return urls
    return [
        "https://google.com",
        "https://youtube.com",
        "https://wikipedia.org",
        "https://microsoft.com",
        "https://apple.com",
        "https://github.com",
        "https://reddit.com",
        "https://nytimes.com",
        "https://cnn.com",
        "https://bbc.com",
    ]


def _load_humansim_websites() -> list[str]:
    path = Path(__file__).resolve().parent / "data" / "websites.txt"
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def _normalize_web_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "://" in value:
        return value
    scheme = "http" if value.endswith(".local") else "https"
    return f"{scheme}://{value}"


def _username_from_host(host_name: str) -> str:
    stem = host_name.replace("-pc", "").replace("-mac", "").replace("nyc", "")
    return stem.replace("-", ".")


def _role_from_host(host_name: str) -> str:
    lowered = host_name.lower()
    if "gabe" in lowered:
        return "it"
    if "jim" in lowered or "pam" in lowered:
        return "sales"
    if "kevin" in lowered:
        return "office"
    return "office"
