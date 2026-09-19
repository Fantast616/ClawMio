ClawMio Linux 发布包

需要：Python 3.11+、Python venv/pip、Bash、可访问 Python 包索引的网络。
此包不是静态二进制或离线依赖包；首次安装会下载 Python 依赖。
应用本身为纯 Python，依赖包由 pip 按 Linux 系统及 CPU 架构选择。

1. 解压后进入本目录。
2. 执行 ./install.sh（不需要 sudo；依赖装入本目录 .venv）。
3. 执行 ./clawmio start。
4. 首次向导填写百炼 API Key、MA 工作空间 ID、区域。
   可选 Base URL 留空即可跳过自助 Skill，先用基础聊天。
5. 查看输出的管理后台地址；执行 ./clawmio password 查看登录密码。

默认监听 127.0.0.1:8000。服务器跨设备访问时，在向导填写 0.0.0.0，
并自行配置防火墙或反向代理；公网推荐 HTTPS。
无桌面环境的服务器无需 --open，使用其他电脑浏览器打开实际服务地址。

常用命令：
  ./clawmio status
  ./clawmio logs -f
  ./clawmio stop
  ./clawmio restart
  ./clawmio configure
  ./clawmio provision --apply
  ./clawmio doctor
  ./clawmio --help

前台运行：./clawmio run，Ctrl+C 停止。
自定义数据目录：./clawmio --home /srv/clawmio start
默认配置与数据库：~/.clawmio，升级时保留并备份整个目录。
升级前 stop，将新发布包解压到新目录执行 install.sh，再 start。
不能在两个目录同时启动同一数据库。

指定 Python：PYTHON_BIN=python3.12 ./install.sh
每次命令使用相同 --home / CLAWMIO_HOME 才会操作同一个实例。
初次准备资源和可选 Skill 需要云端权限；使用百炼可能产生费用。
完整命令、Skill 启停及失败恢复：docs/cli.md
