"""Unit tests for MCP client pure validation functions.

Tests _validate_mcp_command, _validate_sse_url, and _is_private_ip directly.
"""

from __future__ import annotations

import ipaddress
import socket
from unittest.mock import patch

import pytest

from app.mcp.client import _is_private_ip, _validate_mcp_command, _validate_sse_url


class TestValidateMcpCommand:
    def test_validate_mcp_command(self):
        # Empty command
        with pytest.raises(ValueError, match="empty command"):
            _validate_mcp_command("")

        # Safe commands should pass
        _validate_mcp_command("python script.py")
        _validate_mcp_command("/usr/bin/node server.js")
        _validate_mcp_command("python3 -m mymodule")

        # Shell metacharacters
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_mcp_command("cmd; rm -rf /")
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_mcp_command("cmd | cat")
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_mcp_command("cmd && echo")
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_mcp_command("cmd `whoami`")
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_mcp_command("cmd $(whoami)")
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_mcp_command("cmd > file")
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_mcp_command("cmd < file")
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_mcp_command("cmd{}")
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_mcp_command("cmd[]")
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_mcp_command("cmd&")
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_mcp_command("cmd$VAR")

        # Path traversal
        with pytest.raises(ValueError, match="path traversal"):
            _validate_mcp_command("../../../etc/passwd")
        with pytest.raises(ValueError, match="path traversal"):
            _validate_mcp_command("python ../../evil.py")


class TestValidateSseUrlAndIsPrivateIp:
    def test_validate_sse_url(self):
        # Empty URL
        with pytest.raises(ValueError, match="empty URL"):
            _validate_sse_url("")
        with pytest.raises(ValueError, match="empty URL"):
            _validate_sse_url("   ")

        # Bad scheme
        with pytest.raises(ValueError, match="scheme"):
            _validate_sse_url("ftp://example.com")
        with pytest.raises(ValueError, match="scheme"):
            _validate_sse_url("file:///etc/passwd")

        # Private IPv4 addresses (must be blocked)
        with pytest.raises(ValueError, match="private"):
            _validate_sse_url("http://127.0.0.1:3000")
        with pytest.raises(ValueError, match="private"):
            _validate_sse_url("http://10.0.0.1:3000")
        with pytest.raises(ValueError, match="private"):
            _validate_sse_url("http://192.168.1.1:3000")
        with pytest.raises(ValueError, match="private"):
            _validate_sse_url("http://172.16.0.1:3000")
        with pytest.raises(ValueError, match="private"):
            _validate_sse_url("http://169.254.1.1:3000")
        with pytest.raises(ValueError, match="private"):
            _validate_sse_url("http://0.0.0.0:3000")

        # Public IPv4 addresses (must pass)
        _validate_sse_url("http://8.8.8.8")
        _validate_sse_url("https://1.1.1.1")

        # Valid domain names with mocked DNS resolution returning a public IP
        with patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("1.1.1.1", 80))]):
            _validate_sse_url("https://example.com")
            _validate_sse_url("http://example.com:3000")
            _validate_sse_url("https://mcp-server.example.com/sse")

        # Domain name that resolves to a private IP (must be blocked)
        with (
            patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("10.0.0.1", 80))]),
            pytest.raises(ValueError, match="private"),
        ):
            _validate_sse_url("https://internal.example.com")

        # Domain name that cannot be resolved (must raise)
        with (
            patch("socket.getaddrinfo", side_effect=socket.gaierror),
            pytest.raises(ValueError, match="cannot resolve"),
        ):
            _validate_sse_url("https://unresolvable.invalid")

    def test_is_private_ip(self):
        # IPv4 private
        assert _is_private_ip(ipaddress.ip_address("127.0.0.1")) is True
        assert _is_private_ip(ipaddress.ip_address("10.0.0.1")) is True
        assert _is_private_ip(ipaddress.ip_address("192.168.1.1")) is True
        assert _is_private_ip(ipaddress.ip_address("172.16.0.1")) is True
        assert _is_private_ip(ipaddress.ip_address("169.254.1.1")) is True
        assert _is_private_ip(ipaddress.ip_address("0.0.0.0")) is True

        # IPv4 public
        assert _is_private_ip(ipaddress.ip_address("8.8.8.8")) is False
        assert _is_private_ip(ipaddress.ip_address("1.1.1.1")) is False
        assert _is_private_ip(ipaddress.ip_address("203.0.113.1")) is False

        # IPv6 private
        assert _is_private_ip(ipaddress.ip_address("::1")) is True
        assert _is_private_ip(ipaddress.ip_address("fe80::1")) is True
        assert _is_private_ip(ipaddress.ip_address("fc00::1")) is True

        # IPv6 public
        assert _is_private_ip(ipaddress.ip_address("2001:db8::1")) is False
        assert _is_private_ip(ipaddress.ip_address("::ffff:8.8.8.8")) is False
