from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProductizationFilesTest(unittest.TestCase):
    def test_shell_scripts_parse(self) -> None:
        for relative in (
            "scripts/install-gateway.sh",
            "scripts/sanlight-gateway",
            "scripts/release-archive.sh",
        ):
            subprocess.run(
                ["bash", "-n", str(ROOT / relative)],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_current_repository_urls(self) -> None:
        candidates = [
            *ROOT.glob("*.md"),
            *ROOT.glob("docs/*.md"),
            *ROOT.glob("schemas/*.json"),
            ROOT / "systemd/sanlight-mqtt-gateway.service.example",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in candidates)
        self.assertNotIn("github.com/Nibbels/sanlight-mesh-bluez-poc", combined)
        self.assertIn("sanlight-mesh-mqtt-gateway", combined)
        self.assertIn("ioBroker.sanlightmesh", combined)

    def test_schema_ids_and_json(self) -> None:
        schemas = sorted((ROOT / "schemas").glob("*-v1.schema.json"))
        self.assertGreaterEqual(len(schemas), 5)
        for path in schemas:
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                document["$schema"],
                "https://json-schema.org/draft/2020-12/schema",
            )
            self.assertIn("Nibbels/sanlight-mesh-mqtt-gateway", document["$id"])
            self.assertNotIn("sanlight-mesh-bluez-poc", document["$id"])

    def test_installation_safety_is_documented_and_enforced(self) -> None:
        installer = (ROOT / "scripts/install-gateway.sh").read_text(encoding="utf-8")
        self.assertNotIn("set-max", installer)
        self.assertNotIn("set-time", installer)
        self.assertNotIn("blackout", installer)
        self.assertNotIn("--reset-mesh-state", installer)
        self.assertIn("--check", installer)
        self.assertIn("temporary.replace(path)", installer)
        self.assertIn('mqtt_host="127.0.0.1"', installer)
        self.assertIn("allow_anonymous false", installer)
        self.assertNotIn('prompt "MQTT broker host', installer)

    def test_no_split_host_broker_installer_is_documented(self) -> None:
        candidates = [*ROOT.glob("*.md"), *ROOT.glob("docs/*.md")]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in candidates)
        self.assertNotIn("install-mosquitto-broker.sh", combined)
        self.assertNotIn("## External MQTT broker prerequisite", combined)

    def test_management_restart_preserves_arguments(self) -> None:
        helper = (ROOT / "scripts/sanlight-gateway").read_text(encoding="utf-8")
        self.assertIn('args+=(--config "$CONFIG_PATH")', helper)
        self.assertIn('exec sudo -- "$0" "${args[@]}" restart', helper)
        self.assertNotIn('${CONFIG_PATH:+--config "$CONFIG_PATH"}', helper)

    def test_management_helper_includes_manual_mesh_capture_and_recovery(self) -> None:
        helper = (ROOT / "scripts/sanlight-gateway").read_text(encoding="utf-8")
        self.assertIn("capture-mesh-failure NODE [DIR]", helper)
        self.assertIn("recover-mesh", helper)
        self.assertIn('btmon -w "$output/hci.btsnoop"', helper)
        self.assertIn('systemctl stop "$SERVICE"', helper)
        self.assertIn('systemctl restart "$MESH_SERVICE"', helper)
        self.assertIn('wait_mesh_ready 25', helper)
        self.assertIn('systemctl start "$SERVICE"', helper)
        self.assertIn("The Mesh daemon was not restarted", helper)
        self.assertIn("/proc/tty/driver/ttyAMA", helper)
        self.assertIn("Frame reassembly failed", helper)

        capture_start = helper.index("capture_mesh_failure()")
        capture_end = helper.index("status_command()", capture_start)
        capture = helper[capture_start:capture_end]
        self.assertNotIn('restart "$MESH_SERVICE"', capture)
        self.assertNotIn("set-max", capture)
        self.assertNotIn("set-uptime", capture)

    def test_mesh_capture_clears_exit_trap_before_local_scope_ends(self) -> None:
        helper = (ROOT / "scripts/sanlight-gateway").read_text(encoding="utf-8")
        capture_start = helper.index("capture_mesh_failure()")
        capture_end = helper.index("status_command()", capture_start)
        capture = helper[capture_start:capture_end]

        self.assertIn("  cleanup_capture\n  trap - EXIT INT TERM", capture)
        self.assertIn(
            'echo "After a failed probe, recover with: sudo sanlight-gateway recover-mesh"\n\n'
            "  # EXIT traps outlive function-local variables. Complete cleanup while the\n"
            "  # capture scope still exists, then remove the traps before returning.\n"
            "  cleanup_capture\n"
            "  trap - EXIT INT TERM\n",
            capture,
        )

        early_failure = capture.index(
            'echo "ERROR: another SANlight Mesh command is still running." >&2'
        )
        early_return = capture.index("    return 1", early_failure)
        self.assertIn("    cleanup_capture", capture[early_failure:early_return])
        self.assertIn("    trap - EXIT INT TERM", capture[early_failure:early_return])

    def test_uart_overrun_parser_is_awk_portable(self) -> None:
        helper = (ROOT / "scripts/sanlight-gateway").read_text(encoding="utf-8")
        function_start = helper.index("uart_overrun_count()")
        function_end = helper.index("\n}\n\nmesh_health_doctor_summary", function_start)
        function = helper[function_start:function_end]
        marker_start = "  awk '\n"
        marker_end = "\n  ' \"$source\""
        program_start = function.index(marker_start) + len(marker_start)
        program_end = function.index(marker_end, program_start)
        program = function[program_start:program_end]

        completed = subprocess.run(
            ["awk", program],
            input=(
                "serinfo:1.0 driver revision:\n"
                "1: uart:PL011 tx:10 rx:20 oe:40 RTS|CTS|DTR\n"
                "2: uart:PL011 tx:30 rx:40 oe:2 RTS|CTS|DTR\n"
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.strip(), "42")
        self.assertNotIn("for (index =", program)

    def test_management_and_service_include_local_broker(self) -> None:
        helper = (ROOT / "scripts/sanlight-gateway").read_text(encoding="utf-8")
        unit = (ROOT / "systemd/sanlight-mqtt-gateway.service.example").read_text(
            encoding="utf-8"
        )
        self.assertIn('BROKER_SERVICE="mosquitto.service"', helper)
        self.assertIn('systemctl is-active --quiet "$BROKER_SERVICE"', helper)
        self.assertIn('local MQTT broker listening on TCP ${BROKER_PORT}', helper)
        self.assertIn("Requires=mosquitto.service sanlight-meshd-generic.service", unit)
        self.assertIn("After=network-online.target mosquitto.service", unit)

    def test_release_archive_excludes_runtime_material(self) -> None:
        release = (ROOT / "scripts/release-archive.sh").read_text(encoding="utf-8")
        self.assertIn("':(exclude)private'", release)
        self.assertIn("SANlightMesh", release)
        self.assertIn("mqtt-password", release)
        self.assertIn("iobroker-mqtt-password", release)
        self.assertIn("sanlight-mesh-mqtt-gateway", release)
        self.assertIn("sanlight-gateway-diagnostics", release)
        self.assertIn("sanlight-mesh-failure", release)
        self.assertIn("btsnoop", release)

    def test_private_material_not_bundled(self) -> None:
        forbidden_names = {
            "SANlightMesh.json",
            "mqtt-password.txt",
            "iobroker-mqtt-password.txt",
            "sanlight-mesh-mqtt-gateway.passwd",
        }
        try:
            completed = subprocess.run(
                ["git", "-C", str(ROOT), "ls-files", "-z"],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            completed = None

        if completed is not None and completed.returncode == 0:
            paths = (Path(value) for value in completed.stdout.split("\0") if value)
        else:
            # A release archive has no .git metadata. Runtime-private paths are
            # populated only after extraction and are covered by the release
            # exclusions and file-permission tests.
            paths = (
                path.relative_to(ROOT)
                for path in ROOT.rglob("*")
                if path.is_file()
                and not (
                    {".git", "private", ".state"}
                    & set(path.relative_to(ROOT).parts)
                )
            )

        for path in paths:
            self.assertNotIn(path.name, forbidden_names)
            self.assertNotIn(".state", path.parts)


if __name__ == "__main__":
    unittest.main()
