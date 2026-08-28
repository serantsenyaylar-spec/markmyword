#!/usr/bin/env python3
"""Staging smoke test for Mark My Words.

Runs the checks that need a live third-party service. Nothing here can run in a
network-restricted sandbox: it talks to Google, Groq and Supabase for real.

    python scripts/staging_smoke_test.py                 # read-only checks
    python scripts/staging_smoke_test.py --write         # + Sheets row, RLS insert/cleanup
    python scripts/staging_smoke_test.py --apply-migrations
    python scripts/staging_smoke_test.py --only gemini,supabase
    python scripts/staging_smoke_test.py --strict        # skips count as failures

Checks
------
1. gemini    — key is valid, GEMINI_MODEL resolves for that account, one tiny call.
2. groq      — key is valid, GROQ_MODEL resolves for that account, one tiny completion.
3. sheets    — service account authorizes and the target spreadsheet opens;
               with --write, appends one clearly marked SMOKE-TEST row.
4. supabase  — anon key is refused, service-role key works; with --write, RLS is
               proven by inserting as one teacher and confirming another cannot
               read it (using a JWT minted from SUPABASE_JWT_SECRET).
5. oauth     — config passes Streamlit's own validator, the discovery document
               is reachable, and the authorization URL builds. The browser
               round trip itself is manual; the exact steps are printed.

Credentials are read from `.streamlit/secrets.toml` (gitignored) and then
overridden by environment variables of the same name. Never commit real values
and never paste keys into a chat or an issue.

Exit codes: 0 = all ran checks passed, 1 = at least one failed (or, with
--strict, at least one was skipped), 2 = could not start.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRETS_PATH = REPO_ROOT / ".streamlit" / "secrets.toml"
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"

SMOKE_MARKER = "SMOKE-TEST"

_results: list[tuple[str, str, str]] = []


def record(check: str, status: str, detail: str = "") -> None:
    _results.append((check, status, detail))
    print(f"{status:5} {check}  {detail}")


def skip(check: str, reason: str) -> None:
    record(check, "SKIP", reason)


# ----------------------------------------------------------------- config
def load_config() -> dict:
    """secrets.toml first (repo, then any path Streamlit resolves), then env vars."""
    cfg: dict = {}

    def merge_toml(path: Path) -> None:
        try:
            with path.open("rb") as fh:
                cfg.update(tomllib.load(fh))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            print(f"warning: could not read {path}: {exc}", file=sys.stderr)

    if SECRETS_PATH.exists():
        merge_toml(SECRETS_PATH)

    # Streamlit resolves its own list (cwd-based and ~/.streamlit), which is what
    # the running app will actually use, so honour it too.
    try:
        import streamlit.config as st_config

        st_config.get_config_options()
        for candidate in st_config.get_option("secrets.files") or []:
            path = Path(candidate)
            if path.exists() and path.resolve() != SECRETS_PATH.resolve():
                merge_toml(path)
    except Exception as exc:  # noqa: BLE001 - config discovery is best effort
        print(f"warning: could not resolve Streamlit secrets paths: {exc}", file=sys.stderr)

    for key in (
        "GEMINI_API_KEY", "GROQ_API_KEY", "SUPABASE_URL", "SUPABASE_KEY",
        "SUPABASE_ANON_KEY", "SUPABASE_JWT_SECRET", "DATABASE_URL",
        "gcp_service_account", "SHEET_ID", "ALLOWED_DOMAIN", "ADMIN_EMAILS",
        "APP_ENV",
    ):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def require(cfg: dict, check: str, *keys: str) -> bool:
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        skip(check, f"missing {', '.join(missing)}")
        return False
    return True


def app_constants() -> dict:
    """The model names the app actually uses, read from app.py (not hardcoded)."""
    src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    out = {}
    for name in ("GEMINI_MODEL", "GROQ_MODEL"):
        for line in src.splitlines():
            if line.startswith(f"{name} = "):
                out[name] = line.split("=", 1)[1].strip().strip('"').strip("'")
    return out


# ------------------------------------------------------------------ gemini
def check_gemini(cfg: dict, constants: dict) -> None:
    check = "gemini"
    if not require(cfg, check, "GEMINI_API_KEY"):
        return
    model = constants.get("GEMINI_MODEL", "unknown")
    try:
        from google import genai

        client = genai.Client(api_key=cfg["GEMINI_API_KEY"])

        names = set()
        for page in client.models.list():
            names.add((page.name or "").removeprefix("models/"))
        if model in names:
            record(f"{check}: {model} is available for this key", "PASS",
                   f"{len(names)} models listed")
        else:
            near = sorted(n for n in names if "flash" in n)[:5]
            record(f"{check}: {model} is available for this key", "FAIL",
                   f"not in the account's model list; e.g. {near}")
            return

        started = time.monotonic()
        response = client.models.generate_content(
            model=model,
            contents="Reply with the single word: OK",
            # gemini-3.7-flash thinks by default (level "medium") and reasoning
            # tokens count against max_output_tokens. At 8 the model can burn
            # the whole budget on reasoning and return an empty reply, so the
            # check "passes" without actually proving a round trip worked.
            config={"max_output_tokens": 64},
        )
        elapsed = time.monotonic() - started
        text = (response.text or "").strip()
        record(f"{check}: live call succeeds", "PASS",
               f"{elapsed:.2f}s, replied {text[:24]!r}")
    except Exception as exc:  # noqa: BLE001 - report any provider error verbatim
        record(f"{check}: live call succeeds", "FAIL", f"{type(exc).__name__}: {exc}"[:180])


# ------------------------------------------------------------------- groq
def check_groq(cfg: dict, constants: dict) -> None:
    check = "groq"
    if not require(cfg, check, "GROQ_API_KEY"):
        return
    model = constants.get("GROQ_MODEL", "unknown")
    try:
        from groq import Groq

        client = Groq(api_key=cfg["GROQ_API_KEY"])
        ids = {m.id for m in client.models.list().data}
        if model in ids:
            record(f"{check}: {model} is available for this key", "PASS",
                   f"{len(ids)} models listed")
        else:
            near = sorted(i for i in ids if "oss" in i or "llama" in i)[:5]
            record(f"{check}: {model} is available for this key", "FAIL",
                   f"not in the account's model list; e.g. {near}")
            return

        started = time.monotonic()
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word: OK"}],
            max_tokens=8,
        )
        elapsed = time.monotonic() - started
        text = (completion.choices[0].message.content or "").strip()
        record(f"{check}: live call succeeds", "PASS", f"{elapsed:.2f}s, replied {text[:24]!r}")
    except Exception as exc:  # noqa: BLE001
        record(f"{check}: live call succeeds", "FAIL", f"{type(exc).__name__}: {exc}"[:180])


# ------------------------------------------------------------------ sheets
def check_sheets(cfg: dict, write: bool) -> None:
    check = "sheets"
    if not require(cfg, check, "gcp_service_account"):
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        raw = cfg["gcp_service_account"]
        info = json.loads(raw) if isinstance(raw, str) else dict(raw)
        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        record(f"{check}: service account authorizes", "PASS",
               f"account={info.get('client_email', '?')}")

        client = gspread.authorize(creds)
        sheet_id = cfg.get("SHEET_ID")
        if sheet_id:
            spreadsheet = client.open_by_key(sheet_id)
            opened = f"by key {sheet_id[:8]}…"
        else:
            spreadsheet = client.open("İstek_Schools_Grading_Database")
            opened = "by name"
        sheet = spreadsheet.sheet1
        first_row = sheet.row_values(1)
        record(f"{check}: spreadsheet opens ({opened})", "PASS",
               f"{len(first_row)} columns in the header row")

        if not write:
            skip(f"{check}: append a row", "read-only run; pass --write")
            return
        marker_row = [
            f"{SMOKE_MARKER} {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
            "Smoke Test", "smoke@test.invalid", SMOKE_MARKER, "Smoke", 0, 100, 0,
        ]
        sheet.append_row(marker_row)
        last = sheet.row_values(sheet.row_count)
        ok = last and str(last[0]).startswith(SMOKE_MARKER)
        record(f"{check}: append + read back a marked row", "PASS" if ok else "FAIL",
               f"wrote to row {sheet.row_count}: {last[:4]}")
    except Exception as exc:  # noqa: BLE001
        record(f"{check}: spreadsheet access", "FAIL", f"{type(exc).__name__}: {exc}"[:180])


# ---------------------------------------------------------------- supabase
def mint_user_jwt(cfg: dict, email: str) -> str | None:
    """A Supabase-shaped HS256 user token, so RLS can be exercised for real."""
    secret = cfg.get("SUPABASE_JWT_SECRET")
    if not secret:
        return None
    import uuid

    try:
        import jwt as pyjwt
    except ImportError:
        print("warning: PyJWT is not installed; `pip install pyjwt` to run the RLS check",
              file=sys.stderr)
        return None

    now = int(time.time())
    return pyjwt.encode(
        {
            "aud": "authenticated",
            "role": "authenticated",
            "sub": str(uuid.uuid5(uuid.NAMESPACE_URL, f"mailto:{email}")),
            "email": email,
            "iat": now,
            "exp": now + 300,
            "iss": f"{cfg['SUPABASE_URL']}/auth/v1",
        },
        secret,
        algorithm="HS256",
    )


def check_supabase(cfg: dict, write: bool) -> None:
    check = "supabase"
    if not require(cfg, check, "SUPABASE_URL", "SUPABASE_KEY"):
        return
    try:
        from supabase import create_client

        admin = create_client(cfg["SUPABASE_URL"], cfg["SUPABASE_KEY"])
        admin.table("essay_memory").select("id").limit(1).execute()
        record(f"{check}: service-role key can read", "PASS", "RLS bypassed by design")
    except Exception as exc:  # noqa: BLE001
        record(f"{check}: service-role key can read", "FAIL", f"{type(exc).__name__}: {exc}"[:180])
        return

    anon_key = cfg.get("SUPABASE_ANON_KEY")
    if not anon_key:
        skip(f"{check}: anon key is refused", "set SUPABASE_ANON_KEY to test this")
    else:
        try:
            from supabase import create_client as _cc

            anon = _cc(cfg["SUPABASE_URL"], anon_key)
            anon.table("essay_memory").select("id").limit(1).execute()
            record(f"{check}: anon key is refused", "FAIL",
                   "the anon role read data — RLS hardening is not in effect")
        except Exception:  # noqa: BLE001 - any rejection is the expected outcome
            record(f"{check}: anon key is refused", "PASS", "request rejected as expected")

    if not write:
        skip(f"{check}: RLS isolates teachers", "pass --write (inserts and deletes one row)")
        return

    domain = str(cfg.get("ALLOWED_DOMAIN") or "istek.k12.tr").lstrip("@")
    owner = f"smoke.owner@{domain}"
    intruder = f"smoke.intruder@{domain}"
    owner_token, intruder_token = mint_user_jwt(cfg, owner), mint_user_jwt(cfg, intruder)
    if not owner_token or not intruder_token:
        skip(f"{check}: RLS isolates teachers",
             "SUPABASE_JWT_SECRET (and PyJWT) are required to mint user tokens")
        return

    marker = f"{SMOKE_MARKER}-{int(time.time())}"
    try:
        from supabase import create_client as _cc

        as_owner = _cc(cfg["SUPABASE_URL"], cfg["SUPABASE_KEY"])
        as_owner.postgrest.auth(owner_token)
        as_owner.table("essay_memory").insert({
            "student_name": marker, "essay_text": "smoke test row",
            "rubric_type": "Essay", "ai_score": 1, "score": 1,
            "teacher_feedback": marker, "teacher_email": owner,
        }).execute()

        intruder_client = _cc(cfg["SUPABASE_URL"], cfg["SUPABASE_KEY"])
        intruder_client.postgrest.auth(intruder_token)
        leaked = intruder_client.table("essay_memory").select("id").eq("student_name", marker).execute()

        owner_client = _cc(cfg["SUPABASE_URL"], cfg["SUPABASE_KEY"])
        owner_client.postgrest.auth(owner_token)
        visible = owner_client.table("essay_memory").select("id").eq("student_name", marker).execute()

        leaked_rows = leaked.data or []
        visible_rows = visible.data or []
        ok = not leaked_rows and visible_rows
        record(f"{check}: RLS hides one teacher's row from another",
               "PASS" if ok else "FAIL",
               f"intruder saw {len(leaked_rows)} row(s), owner saw {len(visible_rows)}")

        owner_client.table("essay_memory").delete().eq("student_name", marker).execute()
        record(f"{check}: smoke-test row cleaned up", "PASS", marker)
    except Exception as exc:  # noqa: BLE001
        record(f"{check}: RLS isolates teachers", "FAIL", f"{type(exc).__name__}: {exc}"[:180])


def check_migrations(cfg: dict, apply_them: bool) -> None:
    check = "supabase: migrations"
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        record(f"{check}: files present", "FAIL", f"none found in {MIGRATIONS_DIR}")
        return
    record(f"{check}: files present and ordered", "PASS",
           ", ".join(f.name for f in files))

    if not apply_them:
        skip(f"{check}: applied to the project", "pass --apply-migrations")
        return
    if not cfg.get("DATABASE_URL"):
        skip(f"{check}: applied to the project",
             "set DATABASE_URL, or apply with `supabase db push` / the SQL editor")
        return
    try:
        import psycopg  # optional: only needed for --apply-migrations

        with psycopg.connect(cfg["DATABASE_URL"], autocommit=True) as conn:
            for path in files:
                conn.execute(path.read_text(encoding="utf-8"))
                record(f"{check}: applied {path.name}", "PASS")
    except ImportError:
        skip(f"{check}: applied to the project",
             "psycopg is not installed; `pip install psycopg[binary]` or use `supabase db push`")
    except Exception as exc:  # noqa: BLE001
        record(f"{check}: applied to the project", "FAIL", f"{type(exc).__name__}: {exc}"[:180])


# ------------------------------------------------------------------ oauth
def check_oauth(cfg: dict) -> None:
    check = "oauth"
    try:
        import streamlit.config as st_config
        from streamlit.auth_util import validate_auth_credentials
        from streamlit.errors import StreamlitAuthError
        from streamlit.runtime.secrets import secrets_singleton

        st_config.get_config_options()
        secrets_singleton._reset()
        try:
            validate_auth_credentials("google")
        except StreamlitAuthError as exc:
            record(f"{check}: secrets.toml passes Streamlit's validator", "FAIL",
                   " ".join(str(exc).split())[:160])
            return
        record(f"{check}: secrets.toml passes Streamlit's validator", "PASS")

        auth = secrets_singleton.get("auth")
        redirect_uri = auth.get("redirect_uri", "")
        provider = dict(auth.get("google", {}))
        metadata_url = provider.get("server_metadata_url", "")

        # B310: only ever fetch an http(s) URL, whatever the config contains.
        parsed = urlparse(metadata_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            record(f"{check}: discovery document reachable and well-formed", "FAIL",
                   f"refusing non-http(s) server_metadata_url: {metadata_url!r}")
            return

        try:
            import requests  # already a pinned runtime dependency of the app

            response = requests.get(metadata_url, timeout=20)
            response.raise_for_status()
            meta = response.json()
            issuer_ok = meta.get("issuer") == "https://accounts.google.com"
            endpoints_ok = bool(meta.get("authorization_endpoint") and meta.get("token_endpoint"))
            record(f"{check}: discovery document reachable and well-formed",
                   "PASS" if (issuer_ok and endpoints_ok) else "FAIL",
                   f"issuer={meta.get('issuer')}")
            if not (issuer_ok and endpoints_ok):
                return
        except Exception as exc:  # noqa: BLE001
            record(f"{check}: discovery document reachable and well-formed", "FAIL",
                   f"could not fetch {metadata_url}: {type(exc).__name__}: {exc}"[:160])
            return

        from authlib.integrations.requests_client import OAuth2Session

        session = OAuth2Session(
            provider.get("client_id"), provider.get("client_secret"),
            scope=["openid", "email", "profile"], redirect_uri=redirect_uri,
        )
        url, state = session.create_authorization_url(meta["authorization_endpoint"])
        ok = url.startswith(meta["authorization_endpoint"]) and "client_id=" in url and "state=" in url
        record(f"{check}: authorization URL builds from this config", "PASS" if ok else "FAIL",
               f"redirect_uri={redirect_uri}")

        print("\n  Manual browser round trip (the one step this script cannot do):")
        print("    1. Register this exact redirect URI in Google Cloud → APIs & Services")
        print(f"       → Credentials → Authorized redirect URIs: {redirect_uri}")
        print("    2. streamlit run app.py")
        print("    3. Click 'Log in with Google', sign in with a teacher account on the")
        print(f"       allowed domain ({cfg.get('ALLOWED_DOMAIN', 'istek.k12.tr')}).")
        print("    4. Confirm the portal renders, a user_logs row appears, and an")
        print("       account outside the domain is refused at the access-denied screen.")
        print(f"    (state parameter for this session would be: {state[:12]}…)\n")
    except Exception as exc:  # noqa: BLE001
        record(f"{check}: configuration usable", "FAIL", f"{type(exc).__name__}: {exc}"[:180])


# ------------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", action="store_true",
                        help="allow writing: one marked Sheets row, one RLS row (deleted after)")
    parser.add_argument("--apply-migrations", action="store_true",
                        help="apply supabase/migrations/*.sql via DATABASE_URL")
    parser.add_argument("--only", default="",
                        help="comma-separated subset of gemini,groq,sheets,supabase,oauth")
    parser.add_argument("--strict", action="store_true", help="treat SKIP as a failure")
    args = parser.parse_args()

    wanted = {w.strip() for w in args.only.split(",") if w.strip()}
    cfg = load_config()
    if not cfg:
        print(f"No configuration found. Create {SECRETS_PATH} "
              "(copy .streamlit/secrets.toml.example) or export the variables.", file=sys.stderr)
        return 2

    constants = app_constants()
    print(f"Mark My Words staging smoke test — models: "
          f"{constants.get('GEMINI_MODEL', '?')} / {constants.get('GROQ_MODEL', '?')}\n")

    if not wanted or "gemini" in wanted:
        check_gemini(cfg, constants)
    if not wanted or "groq" in wanted:
        check_groq(cfg, constants)
    if not wanted or "sheets" in wanted:
        check_sheets(cfg, args.write)
    if not wanted or "supabase" in wanted:
        check_supabase(cfg, args.write)
        check_migrations(cfg, args.apply_migrations)
    if not wanted or "oauth" in wanted:
        check_oauth(cfg)

    passed = sum(1 for _, s, _ in _results if s == "PASS")
    failed = [(c, d) for c, s, d in _results if s == "FAIL"]
    skipped = [(c, d) for c, s, d in _results if s == "SKIP"]
    print(f"\n{passed} passed, {len(failed)} failed, {len(skipped)} skipped")
    for name, detail in failed:
        print(f"  FAIL  {name} — {detail}")
    for name, detail in skipped:
        print(f"  SKIP  {name} — {detail}")
    if failed:
        return 1
    return 1 if (args.strict and skipped) else 0


if __name__ == "__main__":
    sys.exit(main())
