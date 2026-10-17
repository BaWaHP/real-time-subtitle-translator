# Real-Time Subtitle Translator

Windows 11 x64 实时字幕翻译软件，支持系统声音和 Edge 视频两种模式。

## 安装与启动

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)：

```powershell
winget install --id astral-sh.uv --exact
```

下载并解压项目后，双击 `run.bat` 启动。首次启动会安装 Python 和依赖，语音模型则在第一次识别时下载。如果启动失败，关闭软件后运行 `setup.bat`，再尝试启动。

也可在项目目录手动安装运行：

```powershell
uv python install 3.11
uv venv --python 3.11 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe main.py
```

## 使用

点击 **Continue with ChatGPT** 登录后，刷新模型列表，选择翻译模型，并将翻译模式设为 **ChatGPT**。如需检查连接，可在“连接诊断”页输入一句英文试译。

暂不登录时，可以选择 **Mock** 模式测试英文识别和字幕显示。

### 系统声音

选择“系统声音”和正在使用的扬声器或耳机，点击 **Start** 后播放英文内容。处理过程中可随时暂停、恢复，或点击 **Stop** 停止。

### Edge 视频

1. 在 Edge 打开 `edge://extensions`，开启“开发人员模式”。点击“加载解压缩的扩展”，选择项目中的 `edge_extension` 文件夹。
2. 在软件的“Edge 视频”页复制连接码，再打开视频网页。点击扩展图标，粘贴连接码并选择“连接当前视频”，按提示授权播放器和字幕所在网站。
3. 回到软件，选择已连接的视频，点击“使用此视频并开始”。字幕会跟随播放器的暂停、跳转和倍速变化；刚开始或跳转后，可能需要等待翻译完成。

使用 Panopto 时，需要先开启播放器的英文字幕。如果字幕语言未标明，请确认画面显示英文后，在扩展中勾选确认框。

重启软件后，需要重新复制连接码。更新扩展后，请在扩展页重新加载，并刷新视频网页。

## 设置

- CPU 默认使用 `small.en` / INT8，识别速度跟不上播放时可换成 `base.en` 或 `tiny.en`。
- NVIDIA GPU 默认使用 `medium.en` / FP16，需配置 CUDA 12 cuBLAS 和 cuDNN 9，初始化失败时会改用 CPU。安装方法见 [faster-whisper](https://github.com/SYSTRAN/faster-whisper#gpu)。
- 在“字幕样式”中可调整显示器、字号、颜色、背景和鼠标穿透，关闭穿透后可拖动字幕。

如果 Edge 视频模式无法读取字幕或音轨，可以切换到系统声音模式。字幕延迟会受硬件和网络影响，部分独占全屏游戏也可能无法显示悬浮字幕。

## 测试

```powershell
uv pip install --python .venv\Scripts\python.exe -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
node --test tests/test_extension.mjs tests/test_content_probe.mjs tests/test_background.mjs tests/test_panopto_probe.mjs
```

手动测试时，播放一段英文视频，检查字幕能否正常显示，以及暂停、跳转、倍速和切换视频后是否同步。

许可证：[MIT](LICENSE)。
