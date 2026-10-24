"""deploy/hosted/build.py: the hosted Orchestrator takes its payment rail from .env and carries the Dodo secrets."""

from __future__ import annotations

import base64
import importlib.util
import json
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("hosted_build", ROOT / "deploy" / "hosted" / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

DODO_ENV = {"PAYMENT_RAIL": "dodo", "DODO_PAYMENTS_API_KEY": "k", "DODO_PRODUCT_ID": "pdt_x"}


def test_dodo_rail_is_kept_not_forced_to_free():
    env = dict(DODO_ENV)
    assert build.payment_rail(env) == "dodo" and env["DODO_ENV"] == "test_mode"


def test_rail_defaults_to_dodo_like_core_config():
    with pytest.raises(SystemExit, match="refusing to build the Orchestrator"):
        build.payment_rail({})


@pytest.mark.parametrize("drop", ["DODO_PAYMENTS_API_KEY", "DODO_PRODUCT_ID"])
def test_dodo_without_its_keys_refuses_to_build(drop):
    env = {k: v for k, v in DODO_ENV.items() if k != drop}
    with pytest.raises(SystemExit, match=drop):
        build.payment_rail(env)


def test_free_only_when_asked_and_loudly(capsys):
    assert build.payment_rail({"PAYMENT_RAIL": "free"}) == "free"
    assert "WITHOUT payment" in capsys.readouterr().err


def test_unknown_rail_is_refused():
    with pytest.raises(SystemExit):
        build.payment_rail({"PAYMENT_RAIL": "paypal"})


def test_dodo_secrets_go_to_the_orchestrator_only():
    dodo_keys = {"DODO_PAYMENTS_API_KEY", "DODO_PRODUCT_ID", "DODO_ENV", "AUDIT_PRICE_USD"}
    assert dodo_keys <= set(build.ENTRY["orchestrator"][1])
    for key, (_, secrets) in build.ENTRY.items():
        if key != "orchestrator":
            assert not dodo_keys & set(secrets), key


def bundle_text(code: str) -> str:
    """The embedded module sources of a bundle, decompressed if the builder packed them."""
    if "zlib.decompress" not in code:
        return code
    blob = code.split('"".join("""', 1)[1].split('"""', 1)[0]
    return json.dumps(json.loads(zlib.decompress(base64.b64decode("".join(blob.split())))))


def test_orchestrator_bundle_embeds_the_dodo_rail():
    env = {**build.HOSTED_DEFAULTS, **DODO_ENV, "ASI1_API_KEY": "a"}
    code, missing = build.build_agent("orchestrator", {}, env)
    assert not missing  # optional DODO_RETURN_URL / DODO_SEND_REQUESTPAYMENT are not reported as missing
    orch = bundle_text(code)
    assert "'PAYMENT_RAIL': 'dodo'" in orch and "'DODO_PRODUCT_ID': 'pdt_x'" in orch and "'DODO_ENV': 'test_mode'" in orch
    assert "pdt_x" not in bundle_text(build.build_agent("judge", {}, env)[0])
    with pytest.raises(SystemExit):
        build.build_agent("orchestrator", {}, {**env, "DODO_PRODUCT_ID": ""})
