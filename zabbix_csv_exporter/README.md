# Zabbix CSV Exporter

Dify Tool Plugin，用于把 Zabbix 主机和主机群组导出为两个可下载的 CSV 文件。

Tool 连续返回两个 `create_blob_message`：

- `02_zabbix_hosts_zabbix_export_YYYYMMDD_HHMMSS.csv`
- `03_zabbix_hostgroups_zabbix_export_YYYYMMDD_HHMMSS.csv`

## 安装

项目根目录已经包含打包产物 `zabbix_csv_exporter.difypkg`。在 Dify 页面：

1. 打开“插件”。
2. 选择“安装插件 / 本地文件”。
3. 上传 `zabbix_csv_exporter.difypkg`。
4. 插件安装成功后，再导入 `zabbix信息获取.yml`。

重新打包命令：

```bash
dify-plugin plugin package ./zabbix_csv_exporter
```

## 工作流检查

工作流应为 `User Input → Export Zabbix CSV → Output`。

Output 节点包含：

- `result`：选择 Tool 节点的 `text`。
- `zabbix_files`：选择 Tool 节点的 `files`，类型为 `array[file]`。

运行成功后，`zabbix_files` 应包含两个文件对象，并在结果页面提供下载入口。
