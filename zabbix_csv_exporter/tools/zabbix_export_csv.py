from __future__ import annotations

import csv
import datetime
import io
import json
import re
import urllib.error
import urllib.request
from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage


class ZabbixExportCsvTool(Tool):
    TIMEOUT = 20

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        zabbix_url = self._normalize_text(tool_parameters.get("zabbix_url"))
        zabbix_user = self._normalize_text(tool_parameters.get("zabbix_user"))
        zabbix_password = self._normalize_text(tool_parameters.get("zabbix_password"))

        now = datetime.datetime.now()
        exported_at = now.strftime("%Y-%m-%d %H:%M:%S")
        snapshot_id = "zabbix_export_" + now.strftime("%Y%m%d_%H%M%S")
        hosts_filename = f"02_zabbix_hosts_{snapshot_id}.csv"
        groups_filename = f"03_zabbix_hostgroups_{snapshot_id}.csv"

        missing = [
            name
            for name, value in (
                ("zabbix_url", zabbix_url),
                ("zabbix_user", zabbix_user),
                ("zabbix_password", zabbix_password),
            )
            if not value
        ]
        if missing:
            yield self.create_text_message(self._failure_summary(
                snapshot_id,
                exported_at,
                zabbix_url,
                "参数校验失败",
                "缺少必填参数：" + ", ".join(missing),
            ))
            return

        if not zabbix_url.endswith("api_jsonrpc.php"):
            zabbix_url = zabbix_url.rstrip("/") + "/api_jsonrpc.php"

        def safe_error(error: Exception) -> str:
            detail = str(error)
            if zabbix_password:
                detail = detail.replace(zabbix_password, "***")
            return detail[:1000]

        def post_json(payload: dict[str, Any]) -> Any:
            body = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                zabbix_url,
                data=body,
                headers={
                    "Content-Type": "application/json-rpc",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.TIMEOUT) as response:
                    raw = response.read().decode("utf-8")
            except urllib.error.HTTPError as error:
                raise RuntimeError(f"HTTP {error.code}") from error
            except urllib.error.URLError as error:
                raise RuntimeError(f"连接失败：{error.reason}") from error
            except Exception as error:
                raise RuntimeError(f"请求失败：{type(error).__name__}: {safe_error(error)}") from error

            try:
                data = json.loads(raw)
            except Exception as error:
                raise RuntimeError("Zabbix 返回内容不是合法 JSON") from error
            if "error" in data:
                raise RuntimeError("Zabbix API Error: " + json.dumps(data["error"], ensure_ascii=False))
            if "result" not in data:
                raise RuntimeError("Zabbix API 返回中没有 result 字段")
            return data["result"]

        def zabbix_call(method: str, params: dict[str, Any], auth_token: str | None = None, request_id: int = 1):
            payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": request_id,
            }
            if auth_token:
                payload["auth"] = auth_token
            return post_json(payload)

        try:
            try:
                auth_token = zabbix_call(
                    "user.login",
                    {"username": zabbix_user, "password": zabbix_password},
                    request_id=1,
                )
            except Exception:
                auth_token = zabbix_call(
                    "user.login",
                    {"user": zabbix_user, "password": zabbix_password},
                    request_id=11,
                )
        except Exception as error:
            yield self.create_text_message(self._failure_summary(
                snapshot_id, exported_at, zabbix_url, "Zabbix 登录失败", safe_error(error)
            ))
            return

        host_output = ["hostid", "host", "name", "status", "maintenance_status", "proxyid"]
        interfaces = ["interfaceid", "ip", "dns", "port", "type", "main", "useip"]
        templates = ["templateid", "name"]
        host_params_new = {
            "output": host_output,
            "selectInterfaces": interfaces,
            "selectHostGroups": ["groupid", "name"],
            "selectParentTemplates": templates,
            "sortfield": "host",
        }
        host_params_old = {
            "output": host_output,
            "selectInterfaces": interfaces,
            "selectGroups": ["groupid", "name"],
            "selectParentTemplates": templates,
            "sortfield": "host",
        }

        try:
            try:
                hosts = zabbix_call("host.get", host_params_new, auth_token, 2) or []
            except Exception:
                hosts = zabbix_call("host.get", host_params_old, auth_token, 22) or []
        except Exception as error:
            yield self.create_text_message(self._failure_summary(
                snapshot_id, exported_at, zabbix_url, "获取 Zabbix 主机失败", safe_error(error)
            ))
            return

        group_params_with_uuid = {
            "output": ["groupid", "name", "flags", "uuid"],
            "selectHosts": "count",
            "sortfield": "name",
        }
        group_params_without_uuid = {
            "output": ["groupid", "name", "flags"],
            "selectHosts": "count",
            "sortfield": "name",
        }
        try:
            try:
                hostgroups = zabbix_call("hostgroup.get", group_params_with_uuid, auth_token, 3) or []
            except Exception:
                hostgroups = zabbix_call("hostgroup.get", group_params_without_uuid, auth_token, 33) or []
        except Exception as error:
            yield self.create_text_message(self._failure_summary(
                snapshot_id, exported_at, zabbix_url, "获取 Zabbix 主机群组失败", safe_error(error)
            ))
            return

        host_rows = self._build_host_rows(hosts)
        group_rows = self._build_group_rows(hostgroups)
        hosts_csv = self._csv_bytes(host_rows, [
            "hostid", "host", "name", "status", "status_text", "maintenance_status",
            "proxyid", "primary_ip", "all_ips", "interface_count", "interfaces_summary",
            "groupids", "group_names", "template_names",
        ])
        groups_csv = self._csv_bytes(group_rows, [
            "groupid", "name", "host_count", "top_group", "path_depth",
            "normalized_name", "flags", "uuid",
        ])
        enabled_hosts = sum(1 for row in host_rows if row.get("status") == "0")
        disabled_hosts = sum(1 for row in host_rows if row.get("status") == "1")
        hosts_without_ip = sum(1 for row in host_rows if not row.get("primary_ip"))
        hosts_without_group = sum(1 for row in host_rows if not row.get("group_names"))

        result = (
            "# Zabbix 数据导出完成\n\n"
            f"- 巡检批次：{snapshot_id}\n"
            f"- 导出时间：{exported_at}\n"
            f"- Zabbix 主机数量：{len(host_rows)}\n"
            f"- 启用主机：{enabled_hosts}\n"
            f"- 停用主机：{disabled_hosts}\n"
            f"- 未识别到主 IP 的主机：{hosts_without_ip}\n"
            f"- 未识别到主机群组的主机：{hosts_without_group}\n"
            f"- Zabbix 主机群组数量：{len(group_rows)}\n"
            f"- 主机 CSV：{hosts_filename}\n"
            f"- 主机群组 CSV：{groups_filename}"
        )
        yield self.create_text_message(result)
        yield self.create_blob_message(
            blob=hosts_csv,
            meta={"mime_type": "text/csv", "filename": hosts_filename},
        )
        yield self.create_blob_message(
            blob=groups_csv,
            meta={"mime_type": "text/csv", "filename": groups_filename},
        )

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return "" if value is None else str(value).strip()

    @classmethod
    def _normalize_name(cls, value: Any) -> str:
        return re.sub(r"\s+", " ", cls._normalize_text(value).lower())

    @classmethod
    def _trim_cell(cls, value: Any, max_len: int = 1200) -> str:
        text = cls._normalize_text(value)
        return text if len(text) <= max_len else text[:max_len] + "...[trimmed]"

    @staticmethod
    def _csv_bytes(rows: list[dict[str, Any]], fieldnames: list[str]) -> bytes:
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue().encode("utf-8")

    @classmethod
    def _build_host_rows(cls, hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for host in hosts:
            interfaces = host.get("interfaces", []) or []
            groups = host.get("hostgroups") or host.get("groups") or []
            templates = host.get("parentTemplates", []) or []
            ips = [cls._normalize_text(item.get("ip")) for item in interfaces if cls._normalize_text(item.get("ip"))]
            main_ips = [
                cls._normalize_text(item.get("ip"))
                for item in interfaces
                if cls._normalize_text(item.get("main")) == "1" and cls._normalize_text(item.get("ip"))
            ]
            interface_summary = [
                "ip={}|dns={}|port={}|type={}|main={}|useip={}".format(
                    cls._normalize_text(item.get("ip")),
                    cls._normalize_text(item.get("dns")),
                    cls._normalize_text(item.get("port")),
                    cls._normalize_text(item.get("type")),
                    cls._normalize_text(item.get("main")),
                    cls._normalize_text(item.get("useip")),
                )
                for item in interfaces
            ]
            groupids = [cls._normalize_text(item.get("groupid")) for item in groups if cls._normalize_text(item.get("groupid"))]
            group_names = [cls._normalize_text(item.get("name")) for item in groups if cls._normalize_text(item.get("name"))]
            template_names = [
                cls._normalize_text(item.get("name"))
                for item in templates
                if cls._normalize_text(item.get("name"))
            ]
            status = cls._normalize_text(host.get("status"))
            rows.append({
                "hostid": cls._normalize_text(host.get("hostid")),
                "host": cls._normalize_text(host.get("host")),
                "name": cls._normalize_text(host.get("name")),
                "status": status,
                "status_text": "enabled" if status == "0" else "disabled" if status == "1" else "",
                "maintenance_status": cls._normalize_text(host.get("maintenance_status")),
                "proxyid": cls._normalize_text(host.get("proxyid")),
                "primary_ip": main_ips[0] if main_ips else ips[0] if ips else "",
                "all_ips": cls._trim_cell(";".join(ips), 1200),
                "interface_count": len(interfaces),
                "interfaces_summary": cls._trim_cell(";".join(interface_summary), 2000),
                "groupids": cls._trim_cell(";".join(groupids), 1200),
                "group_names": cls._trim_cell(";".join(group_names), 2000),
                "template_names": cls._trim_cell(";".join(template_names), 2000),
            })
        return rows

    @classmethod
    def _build_group_rows(cls, hostgroups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for group in hostgroups:
            group_name = cls._normalize_text(group.get("name"))
            parts = [part for part in group_name.split("/") if part]
            hosts_count = group.get("hosts", "")
            rows.append({
                "groupid": cls._normalize_text(group.get("groupid")),
                "name": group_name,
                "host_count": len(hosts_count) if isinstance(hosts_count, list) else cls._normalize_text(hosts_count),
                "top_group": parts[0] if parts else group_name,
                "path_depth": len(parts) if parts else 1,
                "normalized_name": cls._normalize_name(group_name),
                "flags": cls._normalize_text(group.get("flags")),
                "uuid": cls._normalize_text(group.get("uuid")),
            })
        return rows

    @staticmethod
    def _failure_summary(snapshot_id: str, exported_at: str, zabbix_url: str, message: str, detail: str) -> str:
        return (
            "# Zabbix 数据导出失败\n\n"
            f"- 巡检批次：{snapshot_id}\n"
            f"- 导出时间：{exported_at}\n"
            f"- Zabbix 地址：{zabbix_url or '未提供'}\n"
            f"- 错误：{message}\n"
            f"- 错误摘要：{detail}"
        )
