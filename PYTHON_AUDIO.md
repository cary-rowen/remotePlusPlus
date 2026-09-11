# Python 音频实现与验证记录

工作目录：`D:\git\my\remotePlusPlus-python`。原目录
`D:\git\my\remotePlusPlus` 保留不动。版本号仍为 0.5.0；没有提交、打标签、发布或安装。

## 实现

音频现在运行在 NVDA 的后台线程中，不再启动 Rust EXE，也不依赖独立 Python 解释器。
插件包不包含 EXE、DLL 或 PYD。依赖均来自 NVDA 和 Windows，因此没有新增依赖或子模块。

* `audio.py`：保留原有设置、协商信封和代次校验，改为管理 Python 工作线程。
* `audioCom.py`：采集和播放共用的 COM 生命周期；异常路径先清理回溯中的设备引用，
  再反初始化，跨线程只保存格式化后的错误诊断。
* `audioCapture.py`：复用 NVDA 自带 pycaw 的设备枚举与 IAudioClient，只补充
  IAudioCaptureClient。系统声音使用 WASAPI loopback，麦克风使用 WASAPI capture。
  COM 对象在采集线程内创建、使用和释放，错误也会释放已取得的缓冲区。
* `audioRuntime.py`：双源固定增益混音、5 ms 分包、40 ms 采集队列、可选预缓冲、
  NVDA WavePlayer 播放、静音清空和中断恢复。远程音频独立于本机提示音音量，保留前导静音。
  单源无需逐采样处理；双源使用标准库
  大整数的批量位运算求 PCM16 平均值，有边界值及随机样本对照测试。
* `audioTransport.py`：有界 TCP 握手、UDP 注册重试、双方心跳、IPv4/IPv6、会话和
  帧长度校验、64 位序号回绕、连接关闭和超时处理。

采样率及声道转换由 Windows 的共享音频引擎处理，使用 AUTOCONVERTPCM 与
SRC_DEFAULT_QUALITY；播放转换由 NVDA 现有实现完成。没有采用先前草稿中有混叠风险的
Python 最近邻重采样。活动音频期间请求 1 ms 多媒体计时精度，退出时配对释放。
这仍是源码形式的插件，但不意味着 Windows/NVDA 本身没有原生实现。

保留原有的四种音质、五档缓冲、系统/麦克风独立开关、单控制方所有权、NVDA 语音覆盖
判断、远程静音及旧代次事件过滤。500 ms 未收到有效 PCM 时恢复远程语音；有效的静音
PCM 仍算媒体。默认采集设备切换或失效时停止音频并报错，重新打开菜单项使用新设备。

兼容基线为 NVDA 2026.1 / Python 3.13，已核对该版本自带 comtypes 1.4.13 与
pycaw 20251023。实际设备验证使用 Windows 10 19045、NVDA 2026.3beta1 的音频 DLL
及本机 NVDA 开发环境。pycaw 接口导入路径、COM 的 `_iid_` 元数据及 NVDA WavePlayer
的 feed/onDone/stop 语义是需要随 NVDA 升级检查的依赖点，分别集中在采集和播放模块。

## 检查

* 使用标准 Python 运行单元测试时，真实 wx 面板及 comtypes/pycaw 测试按环境条件跳过。
  使用 NVDA 开发环境运行时，64 项测试全部通过（含真实 wx 控件）。
* Ruff 检查、格式检查、compileall、`git diff --check`。
* 资源释放修复后 `uv run python -m SCons` 构建成功，检查包内五个音频源文件与工作区完全一致，
  包内没有 EXE/DLL/PYD。`uv run python -m SCons pot` 提取成功。
  本次没有增加翻译字符串，因此没有批量合并或重新翻译 PO 文件。
* `tools/audio_device_probe.py` 使用真实 NVDA WavePlayer 和 Windows 音频设备，
  仅替换外围 UI/config 环境。系统/麦克风四种格式均读到 PCM，播放/静音/停止成功。
* 原版服务器在本机运行，完成三轮 TCP/UDP → 真实 NVDA 播放 → 静音/恢复 →
  发布方离开 → 500 ms 媒体回退 → 停止的验证。没有遗留工作线程。
* 严格 Pyright 已运行，尚有 11 项诊断：COM 动态声明/元数据、未完整标注的
  comtypes 接口，以及当前仓库的默认参数、重复运行时校验和 object.__init__ 规则。
  不将 Pyright 计为通过项，也没有关闭项目的严格规则。

本机直接调用 scons/pyright 启动器会报 `Failed to canonicalize script path`，
使用 `uv run python -m SCons` / `uv run python -m pyright` 可执行。
`[tool.uv] package = false` 声明这是由 SCons 打包的插件工程，避免复制仓库后
uv 将其误当作需要自动发现 Python 包的 wheel 工程。没有改依赖版本。

## 资源释放修复

* 设备 ID 查询保留 `IMMDevice::GetId` 的原始 LPWSTR 指针，复制字符串后在 finally 中
  调用 CoTaskMemFree。此处使用 Windows 标准 COM vtable 的第 5 槽，避免 pycaw 自动转成
  Python 字符串后丢失原始分配地址。首次查询和每秒轮询共用该封装。
* WavePlayer.close 抛错时也执行 COM 反初始化。公共 COM 上下文先清除异常链中已退出
  栈帧的局部引用，保证原生播放器销毁发生在所属线程的 COM 反初始化之前；保留回溯位置，
  跨线程保存格式化后的诊断文本，不保存持有设备对象的原异常。
* 音频服务保存当前及尚未退出的旧代次线程，卸载时禁止新启动，并在锁外等待清理完成。
  普通停止仍异步执行。回归测试经过完整 RemoteService.terminate 调用链，覆盖延迟设备释放、
  启动回调中卸载及线程启动失败。
* DNS 解析与 TCP 连接共用五秒启动期限，等待解析结果时每 50 ms 检查取消。
  同一模块最多运行一个独立 DNS 守护线程；系统 getaddrinfo 本身不能被 Python 强制取消，
  但它不持有音频运行时、设备或连接密钥，迟到结果被丢弃，音频线程退出不等待它。
  系统解析结束前的新查询仍受取消与期限限制，不会不断创建解析线程。

修复后，系统输出和麦克风各进行三轮、每轮 5 万次设备 ID 查询，返回值均一致，
各轮进程私有内存增量均为 0；修复前同类查询每轮增长约 6.6 MB。
四种格式的本机真实采集、NVDA WavePlayer 播放、静音和停止复测通过。
真实 NVDA WavePlayer 的原生停止异常注入也通过：正常和异常路径在 COM 退出时的
存活播放器数均为 0。DNS 阻塞期间卸载、超时、迟到结果和失败恢复均有回归测试，
localhost、IPv4 和 IPv6 的真实解析结果与标准库一致。
Ruff、格式和编译检查通过；COM 初始化集中后，Pyright 原有诊断由 12 项减少至 11 项。
这次修复重新构建了插件包；没有安装，也没有重跑公网或双机测试。

## 性能实测

原版服务端来自 https://github.com/haitun001/NVDARemoteAudioServer 。
双源同时采集，每种实现/音质测试 15 秒；CPU 是发布进程占单个逻辑核的比例。
下表的间隔是本机接收音频包的间隔，不是远程键盘到扬声器延迟。

| 音质 | Rust CPU | Python CPU | Rust 间隔 P95 | Python 间隔 P95 | 丢包 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 48 kHz 立体声 | 1.77% | 1.67% | 10.32 ms | 5.88 ms | 双方 0 |
| 48 kHz 单声道 | 3.02% | 1.87% | 10.17 ms | 5.95 ms | 双方 0 |
| 24 kHz 单声道 | 1.87% | 0.94% | 10.63 ms | 5.88 ms | 双方 0 |
| 16 kHz 单声道 | 1.25% | 2.19% | 10.60 ms | 5.84 ms | 双方 0 |

16 kHz 另做 25 秒复测：双方 CPU 均为 1.62%，双方零丢包，Rust/Python 间隔
P95 分别为 10.56/5.94 ms。数据存在调度波动，不能据此承诺所有设备和音质下 CPU
绝不增加。Windows 音频引擎进程的 CPU 不包含在上表内，转换由 Rust 移至 Windows
也会转移部分计算成本。没有测量完整 NVDA 的 GIL 竞争、长期运行或双机端到端延迟。

公网 www.zxrjy.net:6838 上已验证真实采集音频转发。公网测试双方均出现丢包、较长
到包间隔，部分握手超时，因此不用于判断实现快慢。提供的 6388 端口实测连接超时，
当前保留原插件和服务端源码默认的 6838。测试始终使用随机隔离房间，不加入现有
远程控制房间，不发送键盘操作，不保存捕获音频或连接密码。

## 重现

```powershell
uv run ruff check addon/globalPlugins/remotePlusPlus/audio*.py tests tools sconstruct
uv run python -m unittest discover -s tests
D:\git\nvda\.venv\Scripts\python.exe -m unittest discover -s tests
D:\git\nvda\.venv\Scripts\python.exe tools/audio_device_probe.py
uv run python -m SCons
```

对照脚本需使用已有 comtypes/pycaw 的 NVDA 开发 Python；提供音频服务器地址，
可选 `--rust` 指向旧版 EXE，脚本自动生成隔离房间：

```powershell
D:\git\nvda\.venv\Scripts\python.exe tools/audio_probe.py --host 127.0.0.1 --port 16388 --seconds 15 --rust .validation/rust-baseline/audioClient/target/x86_64-pc-windows-msvc/release/remotePlusPlusAudio.exe
```

`.validation/` 仅存放本地对照代码、旧草稿和原始验证输出，被 Git 和插件构建忽略。
没有修改或安装当前正在使用的插件。正式替换前仍需两台运行 NVDA 的机器实际试听，
并验证目标设备的拔插、默认设备切换和长时间运行。
