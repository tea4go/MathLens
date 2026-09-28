#!/usr/bin/env python3
"""
MathLens 端到端流水线

输入：一张数学题照片
输出：一支带配音的 Manim 教学视频 (mp4)

用法：
    python pipeline.py <题目照片> [-o 输出目录] [-q 质量档位]

示例：
    python pipeline.py resource/input.png
    python pipeline.py 题目.jpg -o output/我的题目 -q h

流程：
    ① 准备工作目录
    ② claude -p 读照片 → 分镜.md + audio_list.csv      [LLM]
    ③ generate_tts.py  → audio/*.wav + audio_info.json  [脚本]
    ④ validate_audio.py → 回写时长 + 打印同步点          [脚本]
    ⑤ claude -p 读分镜 + 同步点 → script.py             [LLM]
    ⑥ check.py                                          [脚本]
    ⑦ manim 渲染 → output.mp4（失败回喂错误重试）        [脚本 + LLM]
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "scripts"
SCAFFOLD = ROOT / "templates" / "script_scaffold.py"
VENV_BIN = Path(sys.executable).parent

STORYBOARD_FILE = "分镜.md"
CSV_FILE = "audio_list.csv"
SCRIPT_FILE = "script.py"
SCENE_CLASS = "MathScene"


# ========== 输出工具 ==========

def log(msg):
    print(f"    {msg}", flush=True)


def section(title):
    print(f"\n{'=' * 66}\n  {title}\n{'=' * 66}", flush=True)


def die(msg):
    print(f"\n✗ {msg}\n", flush=True)
    sys.exit(1)


# ========== 环境准备 ==========

def setup_ffmpeg():
    """确保 manim 能找到 ffmpeg：优先系统安装（通常带 ffprobe），否则用 imageio-ffmpeg 自带的"""
    # winget 安装的 ffmpeg 落在 Links 目录，该目录常常不在非交互式会话的 PATH 里
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        links = Path(local_appdata) / "Microsoft" / "WinGet" / "Links"
        if (links / "ffmpeg.exe").exists():
            os.environ["PATH"] = str(links) + os.pathsep + os.environ.get("PATH", "")

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        log(f"使用系统 ffmpeg: {system_ffmpeg}")
        return

    try:
        import imageio_ffmpeg
    except ImportError:
        die("未找到 ffmpeg，也未安装 imageio-ffmpeg。\n"
            "请安装系统 ffmpeg，或运行：uv pip install imageio-ffmpeg")

    real = Path(imageio_ffmpeg.get_ffmpeg_exe())
    alias = VENV_BIN / "ffmpeg.exe"
    if not alias.exists():
        shutil.copy2(real, alias)
        log(f"已就位 ffmpeg: {alias}")

    os.environ["PATH"] = str(VENV_BIN) + os.pathsep + os.environ.get("PATH", "")


def call_claude(prompt, cwd, timeout=1800, label="LLM"):
    """调用 claude -p 无头模式执行 LLM 环节，prompt 走 stdin 避免命令行编码问题"""
    claude = shutil.which("claude")
    if not claude:
        die("未找到 claude CLI，请确认已安装 Claude Code 并在 PATH 中")

    cmd = [
        claude, "-p",
        "--allowedTools", "Read,Write,Edit,Glob,Grep",
        "--output-format", "text",
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, input=prompt, cwd=str(cwd), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        die(f"{label} 超时（{timeout}s）")

    log(f"{label} 完成，用时 {time.time() - started:.0f}s")
    if proc.returncode != 0:
        die(f"{label} 失败 (rc={proc.returncode})\n{proc.stderr[:1500]}")
    return proc.stdout or ""


def run(cmd, cwd, timeout=None):
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


# ========== 各步骤 ==========

def step_prepare(workdir):
    section("步骤 1/7 · 准备工作目录")
    for sub in ("audio", "media", "assets"):
        (workdir / sub).mkdir(parents=True, exist_ok=True)
    log(f"工作目录: {workdir}")


def step_storyboard(image, workdir, timeout):
    section("步骤 2/7 · 分析题目并生成分镜（LLM）")
    prompt = f"""你是一位资深的数学教学视频分镜师。请为下面这道数学题制作一支教学视频的分镜。

【题目图片】{image}
请用 Read 工具读取这张图片，看清题目内容。

【工作目录】{workdir}
请在当前工作目录下创建文件。

## 一、先做数学分析
- 读懂题目，明确已知条件和待求结论
- 推导解题所需的数学事实
- 如果是几何题，确定几何模型的构建方法，给出关键点的具体坐标
  （坐标系建议：图形放在 (-5,5) × (-4,4) 区域内，中心尽量靠近原点）

## 二、创建 {STORYBOARD_FILE}
必须严格包含以下三个部分，格式不能变：

# 分镜脚本 - <题目名称>

## 分镜设计

### 第1幕：<幕名>
- 画面：<这一幕画面里出现什么、怎么动>
- 读白：<这一幕要念的话>
- 时长：约 <N> 秒

（第2幕、第3幕…… 依次写完全部幕）

## 音频生成清单

| 幕号 | 文件名 | 读白文本 | 时长 | 说话人 | 情感 |
|------|--------|----------|------|--------|------|
| 1 | audio_001_开场.wav | 大家好！今天我们来... | | xiaoxiao | 平和 |
| 2 | audio_002_看图.wav | 首先我们来看这个图形... | | xiaoxiao | 平和 |

（幕号从 1 开始连续；文件名格式 audio_NNN_描述.wav，NNN 是三位数字）

## 音画节奏预算

| 幕号 | 音频时长 | 动画时长 | 说明 |
|------|----------|----------|------|

## 三、创建 {CSV_FILE}
两列表头为 filename,text，内容必须与上面「音频生成清单」的文件名和读白**逐字一致**：

filename,text
audio_001_开场.wav,"大家好！今天我们来..."

【读白写作要求】
- 口语化，像一对一家教在讲题，适合朗读
- 每幕读白 2-4 句话，用中文标点断句（。！？）
- 绝对不要出现 LaTeX 语法或特殊符号
- 数学符号用中文口语表达：△ABC 写成「三角形ABC」，∠1 写成「角1」，
  ⊥ 写成「垂直」，∥ 写成「平行」，√ 写成「根号」，π 写成「派」，
  1/2 写成「二分之一」

【幕数】4-8 幕，覆盖：开场引入 → 图形建立 → 逐步推导 → 结论总结

请直接创建文件，不要询问确认，不要输出多余解释。
只创建 {STORYBOARD_FILE} 和 {CSV_FILE} 两个文件，不要创建任何其他文件。"""

    call_claude(prompt, workdir, timeout, "分镜生成")

    missing = [f for f in (STORYBOARD_FILE, CSV_FILE) if not (workdir / f).exists()]
    if missing:
        die(f"LLM 未生成预期文件: {', '.join(missing)}")
    log(f"✓ {STORYBOARD_FILE}")
    log(f"✓ {CSV_FILE}")


def step_tts(workdir, voice, timeout):
    section("步骤 3/7 · 生成配音音频（edge-tts）")
    proc = run(
        [sys.executable, str(SCRIPTS_DIR / "generate_tts.py"),
         CSV_FILE, "./audio", "--voice", voice],
        cwd=workdir, timeout=timeout,
    )
    print(proc.stdout or "", flush=True)
    if proc.returncode != 0:
        die(f"TTS 生成失败:\n{proc.stderr[:1500]}")

    info = workdir / "audio" / "audio_info.json"
    if not info.exists():
        die("未生成 audio/audio_info.json")

    import json
    data = json.loads(info.read_text(encoding="utf-8"))
    total_sp = sum(len(f.get("sync_points", [])) for f in data.get("files", []))
    log(f"✓ {data.get('count', 0)} 条音频，总时长 {data.get('total_duration', 0):.1f}s，"
        f"同步点 {total_sp} 个")
    if total_sp == 0:
        log("⚠ 同步点为空，wait_for_narration 将失效")


def step_validate(workdir):
    section("步骤 4/7 · 校验音频并回写分镜时长")
    proc = run(
        [sys.executable, str(SCRIPTS_DIR / "validate_audio.py"),
         STORYBOARD_FILE, "./audio"],
        cwd=workdir,
    )
    print(proc.stdout or "", flush=True)
    if proc.returncode != 0:
        log("⚠ 音频校验有警告（不阻断流程）")


def step_script(workdir, timeout):
    section("步骤 5/7 · 生成 Manim 动画代码（LLM）")
    prompt = f"""你是 Manim 动画工程师，请把分镜脚本实现成可渲染的动画代码。

【工作目录】{workdir}
请在当前工作目录下创建 {SCRIPT_FILE}。

【第一步：阅读以下文件】
1. {STORYBOARD_FILE} —— 分镜脚本，每幕的画面和读白
2. audio/audio_info.json —— 每幕音频的时长和句级同步点 sync_points
3. {SCAFFOLD} —— 脚手架模板，含 MathScene 类的全部工具方法

【第二步：创建 {SCRIPT_FILE}】

结构要求（严格遵守）：
- 从脚手架完整复制 MathScene 类，保留全部工具方法：
  _load_audio_data, add_scene_audio, start_scene_with_audio, end_scene_with_audio,
  wait_until_scene_time, wait_for_narration, get_sync_time, get_sync_time_by_index,
  calculate_geometry, assert_geometry, define_elements, create_subtitle,
  show_subtitle_timed, show_subtitle_with_audio, highlight_element,
  indicate_equal_lines, construct, copy_video_to_root
- 填写 SCENES 数组，每项为 (幕号, 幕名, "音频文件名", 0)
  时长写 0 即可，_load_audio_data() 会自动从 audio_info.json 填充
- 实现 calculate_geometry()：返回所有点的坐标
- 实现 assert_geometry()：验证题目给定的事实（如两边相等、某点在圆上）+ 画布范围
- 实现 define_elements()：创建 Manim 图形对象（点、线、圆、标签）
- 实现 play_scene_1() ... play_scene_N()：每幕一个方法，签名 (self, elements, geometry)
- construct() 保持脚手架原样，不要改动

【音画同步（最关键）】
- 每一幕里用 self.wait_for_narration("关键词") 把动画对齐到读白
- 关键词必须是该幕 sync_points 中某句 text 里**真实出现过的子串**
  请先读 audio_info.json 确认每幕有哪些句子，再挑选关键词
- 不要用 self.wait(时长 - N) 手动估算

【字幕】
- 用 self.show_subtitle_timed("文字", 时长) 或 create_subtitle()，不要自造 Subtitle 类
- 字幕放在底部，避免遮挡图形

【禁止事项】
- 不要在类体内写 config.pixel_width / config.pixel_height / config.frame_rate
  （渲染质量由命令行参数控制，硬编码会让质量设置失效）
- 不要使用 Tex / MathTex（中文字体环境容易失败），一律用 Text()
- 不要引入除 manim / json / os 之外的第三方库

请直接创建文件，不要询问确认，不要输出多余解释。
只创建 {SCRIPT_FILE} 一个文件。"""

    call_claude(prompt, workdir, timeout, "动画代码生成")

    if not (workdir / SCRIPT_FILE).exists():
        die(f"LLM 未生成 {SCRIPT_FILE}")
    log(f"✓ {SCRIPT_FILE}")


def step_check(workdir):
    section("步骤 6/7 · 代码结构检查")
    proc = run(
        [sys.executable, str(SCRIPTS_DIR / "check.py"), SCRIPT_FILE],
        cwd=workdir,
    )
    print(proc.stdout or "", flush=True)
    if proc.returncode != 0:
        log("⚠ 结构检查未通过，继续尝试渲染")


def find_video(workdir):
    media = workdir / "media"
    if not media.exists():
        return None
    videos = [
        p for p in media.rglob("*.mp4")
        if "partial_movie_files" not in p.parts
    ]
    if not videos:
        return None
    return max(videos, key=lambda p: p.stat().st_mtime)


def step_render(workdir, quality, max_retries, timeout):
    section(f"步骤 7/7 · 渲染视频（质量 -q{quality}）")

    for attempt in range(1, max_retries + 1):
        log(f"第 {attempt}/{max_retries} 次渲染...")
        started = time.time()
        try:
            proc = run(
                [sys.executable, "-m", "manim", "-q", quality,
                 SCRIPT_FILE, SCENE_CLASS],
                cwd=workdir, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log(f"渲染超时（{timeout}s）")
            proc = None

        video = find_video(workdir)
        elapsed = time.time() - started

        if proc is not None and proc.returncode == 0 and video:
            log(f"✓ 渲染成功，用时 {elapsed:.0f}s")
            return video

        tail = (proc.stdout or "")[-2000:] if proc else "渲染超时"
        err = (proc.stderr or "")[-2000:] if proc else ""
        log(f"✗ 渲染失败（第 {attempt} 次）")

        if attempt >= max_retries:
            print("\n--- manim 输出尾部 ---", flush=True)
            print(tail, flush=True)
            if err:
                print("\n--- stderr ---", flush=True)
                print(err, flush=True)
            die(f"渲染连续失败 {max_retries} 次，请查看 {workdir / SCRIPT_FILE}")

        log("回喂错误给 LLM 修复...")
        fix_prompt = f"""执行 `manim -q {quality} {SCRIPT_FILE} {SCENE_CLASS}` 渲染失败，报错如下：

```
{err or tail}
```

请修复 {SCRIPT_FILE} 中的问题。要求：
- 只修改 {SCRIPT_FILE}，不要动其他文件
- 只修错误，不要削减已实现的功能
- 常见原因：Manim API 用法错误、变量未定义、坐标越界、文本/字体问题
- 再次强调：不要用 Tex/MathTex，不要在类体内硬编码 config.pixel_width/pixel_height/frame_rate

请直接修改文件，不要询问确认。"""
        call_claude(fix_prompt, workdir, timeout=900, label="修复")

    return None


# ========== 主流程 ==========

def main():
    parser = argparse.ArgumentParser(
        description="MathLens 端到端流水线：数学题照片 → 教学视频",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("image", help="数学题照片路径")
    parser.add_argument("-o", "--output", help="输出目录（默认 output/<图片名>）")
    parser.add_argument("-q", "--quality", default="m",
                        choices=["l", "m", "h", "k"],
                        help="渲染质量 l/m/h/k，默认 m (720p30)")
    parser.add_argument("--voice", default="xiaoxiao",
                        help="配音音色，默认 xiaoxiao")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="渲染失败重试次数，默认 3")
    parser.add_argument("--llm-timeout", type=int, default=1800,
                        help="单次 LLM 调用超时秒数，默认 1800")
    parser.add_argument("--render-timeout", type=int, default=3600,
                        help="单次渲染超时秒数，默认 3600")
    args = parser.parse_args()

    image = Path(args.image).resolve()
    if not image.exists():
        die(f"图片不存在: {image}")

    workdir = Path(args.output).resolve() if args.output \
        else (ROOT / "output" / image.stem).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 66}")
    print("  MathLens 端到端流水线")
    print(f"{'=' * 66}")
    print(f"  题目图片: {image}")
    print(f"  输出目录: {workdir}")
    print(f"  渲染质量: {args.quality}")
    print(f"  配音音色: {args.voice}")

    total_start = time.time()

    setup_ffmpeg()
    step_prepare(workdir)
    step_storyboard(image, workdir, args.llm_timeout)
    step_tts(workdir, args.voice, args.llm_timeout)
    step_validate(workdir)
    step_script(workdir, args.llm_timeout)
    step_check(workdir)
    video = step_render(workdir, args.quality, args.max_retries, args.render_timeout)

    final = workdir / "output.mp4"
    shutil.copy2(video, final)

    print(f"\n{'=' * 66}")
    print("  ✅ 完成")
    print(f"{'=' * 66}")
    print(f"  视频: {final}")
    print(f"  用时: {time.time() - total_start:.0f}s")
    print(f"  中间产物: {workdir}")
    print()


if __name__ == "__main__":
    main()
