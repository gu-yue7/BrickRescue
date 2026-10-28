# rescue_files

救砖文件库。**镜像本身不入库**（见根目录 `.gitignore`），这里只保留目录骨架。

把对应的镜像丢进相应子目录，工具启动时自动识别类型并统计「已找到 / 缺文件」。

| 子目录 | 放什么 | 典型来源 |
|---|---|---|
| `boot/` | `boot.img` | 官方固件包、`fastboot fetch` 备份 |
| `dtbo/` | `dtbo.img` | 同上 |
| `vbmeta/` | `vbmeta.img` / `vbmeta_system.img` | 治 dm-verity 红字、橙色警告 |
| `vendor_boot/` | `vendor_boot.img` | A/B 机型 GKI 内核 |
| `recovery/` | `recovery.img` | 官方 recovery，治「一直进 recovery」 |
| `super/` | `super.img` | 动态分区机型整包还原 |
| `fullpackage/` | 全量线刷包：`*.zip` / `*.tgz` / `*.tar.md5` | 厂商官网 / 官方工具 |
| `edl/` | `prog_firehose_ddr.elf` / `.mbn` | 9008 深度砖用的 firehose 引导 |

分区名和机型强相关，镜像文件名尽量保持官方原名（`boot.img`、`vendor_boot.img`），
工具按文件名前缀匹配分区，改名后可能识别不到，此时用「＋ 自定义」手动指定分区。
