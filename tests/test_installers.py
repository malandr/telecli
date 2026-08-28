"""Tests for WSL installer and release scaffolding."""

from pathlib import Path
import os
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def _make_fake_python3(bin_dir: Path) -> None:
    _write_executable(
        bin_dir / "python3",
        """#!/bin/sh
set -eu
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  target="$3"
  mkdir -p "$target/bin"
  cat > "$target/bin/pip" <<'INNER'
#!/bin/sh
exit 0
INNER
  chmod +x "$target/bin/pip"
  cat > "$target/bin/python" <<'INNER'
#!/bin/sh
exit 0
INNER
  chmod +x "$target/bin/python"
  exit 0
fi
echo "unexpected python3 invocation: $@" >&2
exit 1
""",
    )


def _init_fake_repo(path: Path) -> None:
    path.mkdir()
    (path / "requirements.txt").write_text("", encoding="utf-8")
    (path / ".env.sample").write_text(
        """TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
ALLOWED_TELEGRAM_USERS=
WEB_HOST=127.0.0.1
WEB_PORT=8000
AUTH_REQUIRED=true
AUTH_TOKEN=your_auth_token_here
AI_PROXY_ENABLED=false
AI_PROXY_PROVIDER=gemini-cli
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, text=True)


def test_install_wsl_script_has_help_output():
    """The WSL installer should expose a discoverable CLI surface."""
    script = REPO_ROOT / "scripts" / "install-wsl.sh"

    completed = subprocess.run(
        ["/bin/bash", str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Usage:" in completed.stdout
    assert "--prefix" in completed.stdout
    assert "--repo-url" in completed.stdout
    assert "--ref" in completed.stdout


def test_install_wsl_script_mentions_guided_config_options():
    """The WSL installer should advertise the same guided env setup controls."""
    script = REPO_ROOT / "scripts" / "install-wsl.sh"
    text = script.read_text(encoding="utf-8")

    assert "TELECLI_AUTO_CONFIG" in text
    assert "TELECLI_INSTALL_TELEGRAM_BOT_TOKEN" in text
    assert "TELECLI_INSTALL_AUTH_TOKEN" in text
    assert "TELECLI_INSTALL_START_AT_STARTUP" in text


def test_install_linux_script_has_help_output():
    """The Linux installer should expose the same documented CLI surface."""
    script = REPO_ROOT / "scripts" / "install-linux.sh"

    completed = subprocess.run(
        ["/bin/bash", str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Usage:" in completed.stdout
    assert "--prefix" in completed.stdout
    assert "--repo-url" in completed.stdout
    assert "--ref" in completed.stdout


def test_install_wsl_script_dry_run_reports_install_plan(tmp_path):
    """Dry-run mode should print the install targets without mutating the machine."""
    script = REPO_ROOT / "scripts" / "install-wsl.sh"
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    completed = subprocess.run(
        [
            "/bin/bash",
            str(script),
            "--repo-url",
            "https://github.com/example/telecli.git",
            "--ref",
            "v1.2.3",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "HOME": str(fake_home),
            "TELECLI_DRY_RUN": "1",
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert str(fake_home / ".local" / "share" / "telecli") in completed.stdout
    assert str(fake_home / ".local" / "bin" / "telecli-wsl") in completed.stdout
    assert "git clone --branch v1.2.3 --single-branch https://github.com/example/telecli.git" in completed.stdout


def test_install_linux_script_dry_run_reports_install_plan(tmp_path):
    """Linux dry-run mode should print the install targets and launcher name."""
    script = REPO_ROOT / "scripts" / "install-linux.sh"
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    completed = subprocess.run(
        [
            "/bin/bash",
            str(script),
            "--repo-url",
            "https://github.com/example/telecli.git",
            "--ref",
            "v9.9.9",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "HOME": str(fake_home),
            "TELECLI_DRY_RUN": "1",
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert str(fake_home / ".local" / "share" / "telecli") in completed.stdout
    assert str(fake_home / ".local" / "bin" / "telecli") in completed.stdout
    assert "git clone --branch v9.9.9 --single-branch https://github.com/example/telecli.git" in completed.stdout


def test_install_linux_script_can_seed_env_from_answers(tmp_path):
    """Installer answers should be written into the generated .env file."""
    script = REPO_ROOT / "scripts" / "install-linux.sh"
    source_repo = tmp_path / "source-repo"
    _init_fake_repo(source_repo)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _make_fake_python3(fake_bin)

    prefix = tmp_path / "install-root"

    completed = subprocess.run(
        [
            "/bin/bash",
            str(script),
            "--repo-url",
            str(source_repo),
            "--ref",
            "main",
            "--prefix",
            str(prefix),
            "--skip-system-packages",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TELECLI_AUTO_CONFIG": "1",
            "TELECLI_INSTALL_TELEGRAM_BOT_TOKEN": "123456:ABCDEF",
            "TELECLI_INSTALL_ALLOWED_TELEGRAM_USERS": "111,222",
            "TELECLI_INSTALL_WEB_HOST": "0.0.0.0",
            "TELECLI_INSTALL_WEB_PORT": "8800",
            "TELECLI_INSTALL_AUTH_REQUIRED": "true",
            "TELECLI_INSTALL_AUTH_TOKEN": "super-secret-token",
            "TELECLI_INSTALL_AI_PROXY_ENABLED": "true",
            "TELECLI_INSTALL_AI_PROXY_PROVIDER": "claude-cli",
        },
    )

    assert completed.returncode == 0, completed.stderr

    env_text = (prefix / ".env").read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=123456:ABCDEF" in env_text
    assert "ALLOWED_TELEGRAM_USERS=111,222" in env_text
    assert "WEB_HOST=0.0.0.0" in env_text
    assert "WEB_PORT=8800" in env_text
    assert "AUTH_REQUIRED=true" in env_text
    assert "AUTH_TOKEN=super-secret-token" in env_text
    assert "AI_PROXY_ENABLED=true" in env_text
    assert "AI_PROXY_PROVIDER=claude-cli" in env_text


@pytest.mark.parametrize(
    ("script_name", "launcher_name"),
    [
        ("install-linux.sh", "telecli"),
        ("install-wsl.sh", "telecli-wsl"),
    ],
)
def test_installers_keep_env_private_and_do_not_echo_generated_auth_token(tmp_path, script_name, launcher_name):
    """Generated secrets should stay in the env file, and launcher usage should render the real command name."""
    script = REPO_ROOT / "scripts" / script_name
    source_repo = tmp_path / f"source-repo-{launcher_name}"
    _init_fake_repo(source_repo)

    fake_bin = tmp_path / f"bin-{launcher_name}"
    fake_bin.mkdir()
    _make_fake_python3(fake_bin)

    prefix = tmp_path / f"install-root-{launcher_name}"
    bin_dir = tmp_path / f"launcher-bin-{launcher_name}"
    state_dir = tmp_path / f"state-{launcher_name}"

    completed = subprocess.run(
        [
            "/bin/bash",
            str(script),
            "--repo-url",
            str(source_repo),
            "--ref",
            "main",
            "--prefix",
            str(prefix),
            "--bin-dir",
            str(bin_dir),
            "--state-dir",
            str(state_dir),
            "--skip-system-packages",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TELECLI_AUTO_CONFIG": "1",
            "TELECLI_INSTALL_AUTH_REQUIRED": "true",
        },
    )

    assert completed.returncode == 0, completed.stderr

    env_file = prefix / ".env"
    env_text = env_file.read_text(encoding="utf-8")
    auth_token_line = next(line for line in env_text.splitlines() if line.startswith("AUTH_TOKEN="))
    auth_token = auth_token_line.split("=", 1)[1]

    assert auth_token
    assert auth_token != "your_auth_token_here"
    assert (env_file.stat().st_mode & 0o777) == 0o600
    assert auth_token not in completed.stdout
    assert auth_token not in completed.stderr

    launcher = bin_dir / launcher_name
    invalid = subprocess.run(
        [str(launcher), "bogus"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert invalid.returncode == 1
    assert f"Usage: {launcher_name} {{start|stop|restart|status|logs|url}}" in invalid.stderr
    assert "${LAUNCHER_NAME}" not in invalid.stderr


def test_install_linux_script_can_enable_startup_service(tmp_path):
    """Opting into startup should create and enable a user systemd service."""
    script = REPO_ROOT / "scripts" / "install-linux.sh"
    source_repo = tmp_path / "source-repo"
    _init_fake_repo(source_repo)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _make_fake_python3(fake_bin)
    systemctl_log = tmp_path / "systemctl.log"
    _write_executable(
        fake_bin / "systemctl",
        f"""#!/bin/sh
printf '%s\\n' "$*" >> "{systemctl_log}"
exit 0
""",
    )

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    prefix = fake_home / ".local" / "share" / "telecli"

    completed = subprocess.run(
        [
            "/bin/bash",
            str(script),
            "--repo-url",
            str(source_repo),
            "--ref",
            "main",
            "--skip-system-packages",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "HOME": str(fake_home),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TELECLI_AUTO_CONFIG": "1",
            "TELECLI_INSTALL_START_AT_STARTUP": "true",
            "TELECLI_INSTALL_AUTH_REQUIRED": "false",
        },
    )

    assert completed.returncode == 0, completed.stderr

    service_file = fake_home / ".config" / "systemd" / "user" / "telecli.service"
    service_text = service_file.read_text(encoding="utf-8")
    assert f"WorkingDirectory={prefix}" in service_text
    assert f"ExecStart={prefix / 'venv' / 'bin' / 'python'} -m src.main" in service_text
    assert "WantedBy=default.target" in service_text

    systemctl_calls = systemctl_log.read_text(encoding="utf-8")
    assert "daemon-reload" in systemctl_calls
    assert "enable --user telecli.service" in systemctl_calls
    assert "start --user telecli.service" in systemctl_calls


def test_windows_installer_bootstraps_wsl_install_script():
    """The Windows entrypoint should download the WSL installer and invoke it through wsl.exe."""
    installer = REPO_ROOT / "install-windows.ps1"
    text = installer.read_text(encoding="utf-8")

    assert "[string]$Prefix = '~/.local/share/telecli'" in text
    assert "wsl.exe" in text
    assert "Invoke-WebRequest" in text
    assert "install-wsl.sh" in text
    assert "telecli-wsl start" in text
    assert "--prefix" in text
    assert "$HOME/.local/share/telecli" not in text



def test_release_workflow_publishes_wsl_assets():
    """Tagged releases should publish the Windows and WSL installer assets."""
    workflow = REPO_ROOT / ".github" / "workflows" / "release.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "softprops/action-gh-release" in text
    assert "tags:" in text
    assert "v*" in text
    assert "scripts/install-linux.sh" in text
    assert "install-windows.ps1" in text
    assert "scripts/install-wsl.sh" in text


def test_readme_documents_wsl_install_flow():
    """README should explain the supported Windows via WSL setup path."""
    readme = REPO_ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")

    assert "Linux" in text
    assert "scripts/install-linux.sh" in text
    assert "telecli start" in text
    assert "start at startup" in text.lower()
    assert "Windows (WSL2)" in text
    assert "install-windows.ps1" in text
    assert "telecli-wsl start" in text
