"""Closed foundation contracts and semantic configuration checks, not login tests."""

import copy
import unittest

from memoryking.adapters.viewer_config import validate_config
from memoryking.contracts.viewer import validate_viewer
from memoryking.domain.local import LocalError


def config():
    return {
        "version": 1,
        "enabled": False,
        "tenant": "main",
        "public_origin": "https://viewer.example.invalid",
        "listen_host": "127.0.0.1",
        "listen_port": 8772,
        "transport": "isolated_proxy",
        "github_client_id": "example",
        "github_secret_file": "example-secret-file",
        "tls_cert_file": None,
        "tls_key_file": None,
        "session_idle_seconds": 1800,
        "session_absolute_seconds": 28800,
        "login_seconds": 300,
        "poll_seconds": 10,
        "display_lifetime_seconds": 15,
        "limits": {
            "active_sessions": 5,
            "pending_logins": 64,
            "list_items": 50,
            "detail_bytes": 16384,
            "graph_nodes": 100,
            "graph_edges": 150,
            "response_bytes": 262144,
            "concurrency": 4,
            "request_seconds": 5,
        },
    }


class ViewerContractTests(unittest.TestCase):
    def test_trusted_proxy_requires_explicit_canonical_tailnet_addresses(self):
        valid = {
            **config(),
            "trusted_proxy": {
                "kind": "tailscale_serve_v1",
                "source_ips": ["100.64.0.1", "fd7a:115c:a1e0::1"],
            },
        }
        self.assertEqual(validate_config(valid), valid)
        for sources in (
            [],
            ["127.0.0.1"],
            ["100.64.0.0/10"],
            ["100.64.0.01"],
            ["::1"],
            ["FD7A:115C:A1E0::1"],
            ["100.64.0.1", "100.64.0.1"],
        ):
            with self.subTest(sources=sources), self.assertRaises(LocalError):
                validate_config(
                    {
                        **valid,
                        "trusted_proxy": {"kind": "tailscale_serve_v1", "source_ips": sources},
                    }
                )
        with self.assertRaises(LocalError):
            validate_config(
                {**valid, "transport": "direct_tls", "tls_cert_file": "cert", "tls_key_file": "key"}
            )

    def test_closed_config_origin_tls_and_bounds(self):
        self.assertFalse(validate_config(config())["enabled"])
        for change in (
            {"public_origin": "http://viewer.example.invalid"},
            {"public_origin": "https://viewer.example.invalid/path"},
            {"public_origin": "https://user@viewer.example.invalid"},
            {"public_origin": "https://*.example.invalid"},
            {"public_origin": "https://viewer.example.invalid\n"},
            {"listen_host": "0.0.0.0"},
            {"display_lifetime_seconds": 16},
            {"transport": "direct_tls"},
            {"tls_key_file": "unexpected-key"},
            {"session_idle_seconds": 1800, "session_absolute_seconds": 300},
            {"unrecognized": True},
        ):
            with self.subTest(change=change), self.assertRaises(LocalError):
                validate_config({**config(), **change})
        self.assertTrue(
            validate_config(
                {
                    **config(),
                    "transport": "direct_tls",
                    "tls_key_file": "key",
                    "tls_cert_file": "cert",
                }
            )
        )

    def test_subjects_keys_and_readonly_dtos_are_closed(self):
        command = {
            "version": 1,
            "incarnation": "history",
            "key": "retry",
            "action": "bind",
            "args": {"principal": "phone", "issuer": "https://github.com", "user_id": "12345"},
        }
        validate_viewer("ViewerAdminCommand", command)
        for field, value in (
            ("principal", "phone\n"),
            ("user_id", "12345\n"),
            ("issuer", "https://evil.invalid"),
        ):
            changed = copy.deepcopy(command)
            changed["args"][field] = value
            with self.assertRaises(LocalError):
                validate_viewer("ViewerAdminCommand", changed)
        with self.assertRaises(LocalError):
            validate_viewer("ViewerSubject", {"owner_credential": "not allowed"})
