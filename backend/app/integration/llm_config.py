"""The settings panel's model configuration, and the one place a model is installed.

The manager owns exactly one mutable fact -- which `ModelClient` the agent runtime is
using -- and it never persists a secret. It reuses the Chat Completions *connection*
settings the panel already collected; it shares nothing with the retired structured plan
protocol, because the Supervisor and the domain Agents decide in free-form JSON.

Installing a model means swapping the runtime's client. The graphs are built per
invocation, so the next turn picks the new model up without rebuilding anything.
"""

from __future__ import annotations

import logging
from threading import RLock
from typing import Callable

from pydantic import ValidationError

from app.audit.context import (
    SOURCE_CONFIGURATION_PROBE,
    SOURCE_CONFIGURATION_TEST,
    SOURCE_STARTUP_PROBE,
    AuditContext,
    audit_context,
    current_context,
)
from app.audit.model_client import PROVIDER_OPENAI_SDK, AuditedModelClient
from app.core.errors import AppError
from app.integration.model_client import (
    ChatCompletionsModelClient,
    ModelClient,
    ModelClientError,
    UnconfiguredModelClient,
)
from app.integration.model_config import ChatCompletionsConfig

# The probe is one cheap turn whose only job is to prove the boundary works: the provider
# answered, and the answer carried text the agents could have parsed.
PROBE_MESSAGES: tuple[dict[str, str], ...] = (
    {"role": "system", "content": "只输出一个 JSON 对象。"},
    {"role": "user", "content": '请输出 {"ok":true}'},
)

logger = logging.getLogger(__name__)


def _mask_key(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}...{value[-4:]}"


class LlmConfigManager:
    """Process-local Chat Completions configuration with no secret persistence."""

    def __init__(self, runtime, *, client_factory: Callable | None = None) -> None:
        self._runtime = runtime
        self._client_factory = client_factory or self._build_client
        self._config: ChatCompletionsConfig | None = None
        self._mode = "unconfigured"
        self._lock = RLock()

    def public_snapshot(self) -> dict[str, object]:
        with self._lock:
            config = self._config
            return {
                "configured": config is not None,
                "mode": self._mode,
                "base_url": config.base_url if config else "",
                "model": config.model if config else "",
                "response_format": config.response_format if config else "json_schema",
                "timeout_seconds": config.timeout_seconds if config else 30.0,
                "api_key_masked": (_mask_key(config.api_key.get_secret_value()) if config else ""),
            }

    def test(self, values: dict[str, object]) -> dict[str, object]:
        config = self._candidate_config(values)
        client = self._build_and_probe(config, source=SOURCE_CONFIGURATION_TEST)
        client.close()
        return {"ok": True, "message": "模型连接和输出校验通过"}

    def configure(
        self,
        values: dict[str, object],
        *,
        source: str = SOURCE_CONFIGURATION_PROBE,
        on_change: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        """Install a model, recording the change before it takes effect.

        Nothing here is stored: the configuration lives in memory, so there is no row to
        commit the swap alongside and no shared transaction to lean on. `on_change` -- the
        caller's audit write, in a transaction of its own -- therefore runs after the probe
        has proved the candidate answers and *before* the lock is taken and memory changes,
        and its exception is not caught. A change that cannot be recorded is a change that
        does not happen; that ordering is the whole guarantee.

        The hook deliberately does not run before the probe: a candidate that cannot answer
        leaves the configuration exactly as it was, and an "ok" row for a change that never
        happened would be a false record.

        A hook that fails takes the candidate with it: the probe's client has been built and
        nothing else holds it, so it is closed before the exception leaves, the same way the
        client it replaces is closed on the way in.
        """

        config = self._candidate_config(values)
        client = self._build_and_probe(config, source=source)
        try:
            if on_change is not None:
                on_change()
        except BaseException:
            client.close()
            raise
        with self._lock:
            previous = self._runtime.agent.model
            self._config = config
            self._mode = "model"
            # The running runtime swaps its client; its threads and checkpoints stay put.
            self._runtime.agent.set_model(client)
            if previous is not client:
                previous.close()
            return self.public_snapshot()

    def clear(self, *, on_change: Callable[[], None] | None = None) -> dict[str, object]:
        """Remove the model, recording the change before it takes effect.

        As in `configure`, the configuration lives in memory and there is no shared
        transaction, so `on_change` runs before the lock and the memory change and its
        exception is not caught: a clear that cannot be recorded leaves the installed model
        where it was. The hook sits differently only because there is nothing to prepare
        here -- no candidate to probe, so no risk of an "ok" row for a probe that failed.
        """

        if on_change is not None:
            on_change()
        with self._lock:
            previous = self._runtime.agent.model
            self._config = None
            self._mode = "unconfigured"
            # Clearing the model leaves a loud failure behind, never a rule fallback.
            self._runtime.agent.set_model(UnconfiguredModelClient())
            if not isinstance(previous, UnconfiguredModelClient):
                previous.close()
            return self.public_snapshot()

    def _candidate_config(self, values: dict[str, object]) -> ChatCompletionsConfig:
        candidate = dict(values)
        api_key = candidate.get("api_key")
        if not api_key:
            with self._lock:
                if self._config is None:
                    raise AppError("LLM_CONFIG_INVALID", "请填写 API Key", 422)
                api_key = self._config.api_key.get_secret_value()
            candidate["api_key"] = api_key
        try:
            return ChatCompletionsConfig.model_validate(candidate)
        except ValidationError as error:
            raise AppError(
                "LLM_CONFIG_INVALID",
                "模型配置格式不正确",
                422,
                details={"fields": [item["loc"][0] for item in error.errors()]},
            ) from error

    def _build_client(self, config: ChatCompletionsConfig) -> ModelClient:
        return ChatCompletionsModelClient(config)

    def _audited(self, client: ModelClient, config: ChatCompletionsConfig) -> ModelClient:
        """Wrap an adapter so every call through it is recorded.

        Every path that installs a model goes through here -- the panel's test, the panel's
        install and the environment bootstrap -- so there is exactly one place a client can
        enter the process, and no path can quietly skip the audit boundary.

        A runtime that has no audit service (a unit test with a hand-built runtime) keeps the
        bare adapter: the audit store is a deployment fact, not something to invent.
        """

        audit = getattr(self._runtime, "audit", None)
        if audit is None:
            return client
        return AuditedModelClient(
            client,
            audit,
            provider=PROVIDER_OPENAI_SDK,
            model=config.model,
        )

    def _build_and_probe(self, config: ChatCompletionsConfig, *, source: str) -> ModelClient:
        """Build the candidate client, wrap it for audit, and prove it answers.

        The probe is a real model call like any other, so it goes through the same decorator
        as a turn would: the record says which source asked for it and that no conversation
        was involved. The wrapper is installed *before* the probe runs, which is what makes
        the probe itself auditable rather than only the calls that come after it.
        """

        try:
            client = self._audited(self._client_factory(config), config)
            # The source is *merged* into whatever context the caller established, not
            # substituted for it: the settings route knows which user asked for this probe,
            # and replacing the context here would throw that identity away and leave the
            # record belonging to nobody.
            with audit_context((current_context() or AuditContext()).merged(source=source)):
                reply = client.complete(PROBE_MESSAGES)
            if not isinstance(reply.text, str) or not reply.text.strip():
                raise AppError(
                    "LLM_RESPONSE_INVALID",
                    "模型没有返回内容",
                    502,
                    details={"type": "EmptyReply"},
                )
            return client
        except AppError:
            raise
        except ModelClientError as error:
            # The classified code, not the wrapper's class name: an operator probing a new
            # key needs to see LLM_AUTH rather than "ModelClientError".
            raise AppError(
                "LLM_CONNECTION_FAILED",
                "无法连接模型服务，请检查 Base URL、Key 和模型名",
                502,
                details={"type": error.code},
            ) from error
        except Exception as error:
            raise AppError(
                "LLM_CONNECTION_FAILED",
                "无法连接模型服务，请检查 Base URL、Key 和模型名",
                502,
                details={"type": type(error).__name__},
            ) from error


def configure_from_settings(manager: LlmConfigManager, settings) -> bool:
    """Install the model the environment names, if it names one.

    This is the startup path only, so a process can come up already configured instead of
    waiting for someone to open the settings panel. The panel stays the runtime authority:
    a later `PUT /api/settings/llm` simply replaces what this installed.

    An environment that names no model -- the default, and every checkout without a `.env`
    -- leaves the runtime unconfigured exactly as before, so nothing changes for a caller
    that never opted in.

    A provider that is unreachable must not stop the API from serving, so a failed probe is
    logged and reported through `model_configured` rather than aborting startup. The
    classified cause is logged; the key itself never is.

    The probe is a model call, so it is recorded like one, under the `startup_probe` source:
    a process that came up configured should be able to say what it was configured with and
    that the provider answered.

    @returns whether a model was installed.
    """

    base_url = (settings.llm_base_url or "").strip()
    api_key = (settings.llm_api_key or "").strip()
    model = (settings.llm_model or "").strip()
    if not (base_url and api_key and model):
        return False
    try:
        manager.configure(
            {
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
                "response_format": settings.llm_response_format,
                "timeout_seconds": settings.llm_timeout_seconds,
            },
            source=SOURCE_STARTUP_PROBE,
        )
    except AppError as error:
        logger.error(
            "llm_env_bootstrap_failed code=%s type=%s", error.code, error.details.get("type")
        )
        return False
    logger.info("llm_env_bootstrap_ok base_url=%s model=%s", base_url, model)
    return True
