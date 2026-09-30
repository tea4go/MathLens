#!/usr/bin/env python3
"""
MathLens 端到端流水线

输入：一张数学题照片
输出：一支带配音的 Manim 教学视频 (mp4)

用法：
    python pipeline.py <题目照片> [-o 输出目录] [-q 质量档位] [-g 年级]

示例：
    python pipeline.py resource/input1.png
    python pipeline.py 题目.jpg -o output/我的题目 -q h
    python pipeline.py 题目.jpg -g 八上        # 只用苏科版八年级上册及之前的知识讲

流程：
    ① 准备工作目录
    ② claude -p 读照片 → 分镜.md + audio_list.csv      [LLM]
    ③ 审计解法是否超出所学范围（超范围则带反馈重做②）    [LLM]
    ④ generate_tts.py  → audio/*.wav + audio_info.json  [脚本]
    ⑤ validate_audio.py → 回写时长 + 打印同步点          [脚本]
    ⑥ claude -p 读分镜 + 同步点 → script.py             [LLM]
    ⑦ check.py                                          [脚本]
    ⑧ manim 渲染 → output.mp4（失败回喂错误重试）        [脚本 + LLM]
"""

import argparse
import json
import os
import re
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

# 各学段的可用/禁用数学工具，用来约束 LLM 选用的解法
GRADE_TOOLKITS = {
    "小学": {
        "allowed": "四则运算与运算律、分数/小数/百分数、比与比例、简易方程（五年级起）、"
                   "平面图形的周长与面积（长方形、正方形、三角形、平行四边形、梯形、圆）、"
                   "立体图形的表面积与体积（长方体、正方体、圆柱）、图形的平移/旋转/轴对称、"
                   "用数对表示位置、统计图表与平均数",
        "forbidden": "勾股定理、三角函数、相似三角形、全等三角形的严格证明、"
                     "平面直角坐标系与函数、根号与无理数、向量、"
                     "以及任何需要字母代数式复杂变形的推导",
        "style": "只用具体数值计算，不做一般性代数证明；"
                 "几何结论用「观察 + 度量 + 归纳」的方式说明",
    },
    "初中": {
        "allowed": "实数（含二次根式）、整式与分式运算、一元一次/二元一次/一元二次方程、"
                   "不等式（组）、一次函数/二次函数/反比例函数、平面直角坐标系、"
                   "全等三角形、相似三角形、勾股定理、锐角三角函数（九年级）、"
                   "圆（垂径定理、圆周角、切线）、图形的平移/旋转/轴对称、统计与概率初步",
        "forbidden": "平面向量、导数、三角恒等变换与正弦/余弦定理、复数、空间向量、"
                     "解析几何的直线与圆锥曲线方程",
        "style": "几何证明可用全等/相似/勾股；代数可用方程与函数；"
                 "立体几何不要用向量或坐标法，用传统综合法",
    },
    "高中": {
        "allowed": "集合与常用逻辑、函数与导数、三角函数与恒等变换、解三角形、数列、"
                   "不等式、平面向量、立体几何（含空间向量）、"
                   "解析几何（直线、圆、圆锥曲线）、计数原理与概率统计",
        "forbidden": "大学及以上内容（微积分进阶、线性代数、复变函数等）",
        "style": "优先选与题目所属模块最基础的方法；"
                 "能用初中方法解决的，不必上向量或导数",
    },
}

# 长的关键词要排在前面，否则「高一年级」会先撞上「一年级」
GRADE_KEYS = (
    ("高中", ("高中", "高一", "高二", "高三")),
    ("初中", ("初中", "初一", "初二", "初三", "七年级", "八年级", "九年级",
              "七上", "七下", "八上", "八下", "九上", "九下")),
    ("小学", ("小学", "一年级", "二年级", "三年级", "四年级", "五年级", "六年级")),
)


def resolve_grade(grade):
    """把「初二」「小学五年级」这类输入归到学段；识别不了返回 None"""
    if not grade:
        return None
    g = grade.strip()
    for band, keys in GRADE_KEYS:
        if any(k in g for k in keys):
            return band
    return None


# ========== 知识库（苏科版教材知识点笔记） ==========
#
# 笔记按册存放（七上/七下/…/九下），每册含 `## 第N章 章名` 与 `### N.M 节名` 两级标题。
# 章号在各册间重复，因此骨架必须带册别前缀，否则 LLM 无法判断「第3章」是哪一册的。

KB_DIR_ENV = "MATHLENS_KB_DIR"
KB_DIR_FALLBACK = Path(r"C:\SyncData\WhaleNotes\云上笔记\教育学习")

VOLUME_ORDER = ("7上", "7下", "8上", "8下", "9上", "9下")

VOLUME_FILE_RE = re.compile(r"(\d)\s*年级\s*(上|下)\s*册")
CHAPTER_RE = re.compile(r"^##\s*第(\d+)章\s*(.+?)\s*$")
APPENDIX_RE = re.compile(r"^##\s*附录[：:]\s*(.+?)\s*$")
SECTION_RE = re.compile(r"^###\s*(\d+\.\d+)\s*(.+?)\s*$")

# 具体册别关键词，长的排前面，避免「八上」被「八」抢先
VOLUME_KEYS = (
    ("9下", ("9下", "九下", "九年级下", "初三下")),
    ("9上", ("9上", "九上", "九年级上", "初三上")),
    ("8下", ("8下", "八下", "八年级下", "初二下")),
    ("8上", ("8上", "八上", "八年级上", "初二上")),
    ("7下", ("7下", "七下", "七年级下", "初一下")),
    ("7上", ("7上", "七上", "七年级上", "初一上")),
)

# 学年/学段 → 该阶段结束时所学的最后一册
GRADE_END_VOLUME = (
    ("9下", ("初中", "初三", "九年级", "中考")),
    ("8下", ("初二", "八年级")),
    ("7下", ("初一", "七年级")),
)


def resolve_volumes(grade):
    """把「初二」「八上」展开成累计册别列表（含此前所有册）；识别不了返回 None

    「八上」→ 七上 七下 八上（学生此时已学完初一）
    「初二」→ 七上 七下 八上 八下（学年结束）
    """
    if not grade:
        return None
    g = grade.strip()
    for vol, keys in VOLUME_KEYS + GRADE_END_VOLUME:
        if any(k in g for k in keys):
            return list(VOLUME_ORDER[:VOLUME_ORDER.index(vol) + 1])
    return None


def resolve_kb_dir():
    """知识库目录：环境变量优先，其次默认路径；都不可用返回 None"""
    env = os.environ.get(KB_DIR_ENV)
    if env:
        p = Path(env)
        if p.is_dir():
            return p
        log(f"⚠ {KB_DIR_ENV}={env} 不是有效目录，改用默认路径")
    return KB_DIR_FALLBACK if KB_DIR_FALLBACK.is_dir() else None


def load_kb_index(kb_dir):
    """扫描知识库目录，返回 {册别: {"title": 册名, "outline": 章/节骨架文本}}"""
    index = {}
    for md in sorted(Path(kb_dir).glob("*.md")):
        m = VOLUME_FILE_RE.search(md.name)
        if not m:
            continue
        title, lines = md.stem, []
        for raw in md.read_text(encoding="utf-8").splitlines():
            line = raw.rstrip()
            if line.startswith("# "):
                title = line[2:].strip()
            elif (cm := CHAPTER_RE.match(line)):
                lines.append(f"第{cm.group(1)}章 {cm.group(2)}")
            elif (am := APPENDIX_RE.match(line)):
                lines.append(f"附录：{am.group(1)}")
            elif (sm := SECTION_RE.match(line)):
                lines.append(f"  {sm.group(1)} {sm.group(2)}")
        if lines:
            index[f"{m.group(1)}{m.group(2)}"] = {"title": title, "outline": "\n".join(lines)}
    return index


def build_scope_text(index, volumes):
    """把指定册别的骨架拼成给 LLM 的范围清单"""
    return "\n\n".join(
        f"【{index[v]['title']}】\n{index[v]['outline']}"
        for v in volumes if v in index
    )


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
    section("步骤 1/8 · 准备工作目录")
    for sub in ("audio", "media", "assets"):
        (workdir / sub).mkdir(parents=True, exist_ok=True)
    log(f"工作目录: {workdir}")


def step_storyboard(image, workdir, timeout, grade="不限", kb_scope=None, feedback=""):
    section("步骤 2/8 · 分析题目并生成分镜（LLM）")

    band = resolve_grade(grade)
    if kb_scope:
        grade_section = f"""
## 一、学情约束（最重要，必须遵守）
本视频面向【{grade}】的学生。下面列出该学段**已经学过**的全部教材章节，
解题只能用这些章节里的知识，**不得使用清单之外的任何定理、方法或公式**。

{kb_scope}

【讲解风格】
优先用最基础、最直观的方法；能一步算出来的不要绕两步。

如果这道题确实用清单内的知识解不了，就退一步，选一个最接近该学段、
最基础的方法来讲，并在开场读白里用一句话点明需要补充的新知识，
不要让观众一上来就面对完全陌生的工具。
"""
    elif grade and grade.strip() != "不限":
        if band:
            kit = GRADE_TOOLKITS[band]
            grade_section = f"""
## 一、学情约束（最重要，必须遵守）
本视频面向【{grade}】的学生，解题只能用该学段已经学过的知识。

【可用工具】
{kit['allowed']}

【绝对不要使用】
{kit['forbidden']}

【讲解风格】
{kit['style']}

如果这道题用【{grade}】的知识确实解不了，就退一步，选一个最接近该学段、
最基础的方法来讲，并在开场读白里用一句话点明需要补充的新知识，
不要让观众一上来就面对完全陌生的工具。
"""
        else:
            log(f"⚠ 无法识别年级「{grade}」属于哪个学段，只按字面约束，不附加工具清单")
            grade_section = f"""
## 一、学情约束（必须遵守）
本视频面向【{grade}】的学生，解题只能使用该年级已经学过的知识，
不要使用更高学段的工具和方法。
"""
    else:
        grade_section = """
## 一、学情约束
不限学段。可以用任何恰当的方法，但优先选择最基础、最直观的解法。
"""

    if feedback:
        grade_section = f"""## 零、上一版被打回的原因（必须修正）
上一版分镜用了超出所学范围的知识，请换用清单内的等价方法重做：
{feedback}

""" + grade_section

    prompt = f"""你是一位资深的数学教学视频分镜师。请为下面这道数学题制作一支教学视频的分镜。

【题目图片】{image}
请用 Read 工具读取这张图片，看清题目内容。

【工作目录】{workdir}
请在当前工作目录下创建文件。
{grade_section}
## 二、先做数学分析
- 读懂题目，明确已知条件和待求结论
- 推导解题所需的数学事实
- 如果是几何题，确定几何模型的构建方法，给出关键点的具体坐标
  （坐标系建议：图形放在 (-5,5) × (-4,4) 区域内，中心尽量靠近原点）

## 三、创建 {STORYBOARD_FILE}
必须严格包含以下四个部分，格式不能变：

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

## 解题方法清单

把这道题的解法**拆成一条条独立的数学事实**，逐条列出它用到的定理/方法：
（这一节会被审核，用来确认没有超出学生所学范围，务必如实填写）

| # | 用到的定理/方法 | 教材出处（第几册第几章） | 用在哪一步 |
|---|----------------|------------------------|-----------|
| 1 | 全等三角形 SSS 判定 | 八上 第1章 | 证明 △ABD ≌ △ACE |
| 2 | 勾股定理的逆定理 | 八上 第3章 | 由三边平方和判断直角 |

填写要求：
- 只写**真正用到的**定理/方法，不要罗列无关知识凑数
- 「教材出处」写你判断的册别和章节；不确定就写「不确定」
- 如果用了清单外的知识（比如更高的学段的方法），必须如实写出来，不要隐瞒

## 四、创建 {CSV_FILE}
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


def extract_json(text):
    """从 LLM 输出里抠出第一个 JSON 对象；失败返回 None"""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def audit_storyboard(workdir, index, volumes, timeout):
    """让 LLM 逐条比对「解题方法清单」与教材范围；返回 {"verdict","items"} 或 None"""
    allowed = "、".join(volumes)
    full_map = "\n\n".join(
        f"【{index[v]['title']}】\n{index[v]['outline']}"
        for v in VOLUME_ORDER if v in index
    )

    prompt = f"""你是教材范围审核员。请审核一份数学教学视频分镜，判断它的解法是否超出学生已学范围。

【学生已学范围】{allowed}（共 {len(volumes)} 册）—— 只有这些册里的知识可用
{build_scope_text(index, volumes)}

【完整教材地图】下面是初中六册的全部章节，用来判断某个知识点究竟属于哪一册
{full_map}

【待审核文件】{workdir / STORYBOARD_FILE}
请用 Read 工具读取，重点看「## 解题方法清单」那一节的表格。

【任务】
对表格里的每一条，判断它属于哪一册：
- 属于 {allowed} 中某一册 → ok = true
- 属于更靠后的册（学生还没学）→ ok = false
- 不在教材地图里的方法（超纲技巧、更高学段内容）→ ok = false

【输出】只输出一个 JSON 对象，前后不要有任何其他文字：
{{"verdict": "pass", "items": [{{"method": "勾股定理的逆定理", "volume": "8上", "ok": true, "reason": "八年级上册第3章，已学"}}]}}

判定从严：拿不准的判为 false，并在 reason 里说明拿不准。"""

    result = extract_json(call_claude(prompt, workdir, timeout, "范围审计"))
    if result is None:
        log("⚠ 审计输出无法解析为 JSON")
    return result


def step_storyboard_guarded(image, workdir, timeout, grade, index, volumes, max_retries):
    """生成分镜 → 审计 → 超范围则带反馈重做；未启用知识库时直接生成"""
    kb_scope = build_scope_text(index, volumes) if (index and volumes) else None
    if not kb_scope:
        step_storyboard(image, workdir, timeout, grade)
        return

    feedback = ""
    for attempt in range(max_retries + 1):
        step_storyboard(image, workdir, timeout, grade, kb_scope, feedback)

        section("步骤 3/8 · 审计解法是否超出所学范围（LLM）")
        result = audit_storyboard(workdir, index, volumes, timeout)
        if result is None:
            log("⚠ 跳过审计，沿用当前分镜")
            return

        items = result.get("items", [])
        bad = [i for i in items if not i.get("ok", True)]
        if result.get("verdict") == "pass" and not bad:
            log(f"✓ 范围审计通过（{len(items)} 条方法全部在范围内）")
            return

        log(f"✗ 范围审计未通过，{len(bad)} 条超范围")
        for i in bad:
            log(f"  · {i.get('method', '?')}（{i.get('volume', '?')}）: {i.get('reason', '')}")

        if attempt >= max_retries:
            log(f"⚠ 已重做 {max_retries} 次仍未通过，沿用当前分镜继续（建议人工检查）")
            return

        log(f"回喂审计结果，重做分镜（第 {attempt + 1}/{max_retries} 次）...")
        feedback = "\n".join(
            f"- {i.get('method', '?')}：{i.get('reason', '')}（判断属于 {i.get('volume', '?')}）"
            for i in bad
        )


def step_tts(workdir, voice, timeout):
    section("步骤 4/8 · 生成配音音频（edge-tts）")
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
    section("步骤 5/8 · 校验音频并回写分镜时长")
    proc = run(
        [sys.executable, str(SCRIPTS_DIR / "validate_audio.py"),
         STORYBOARD_FILE, "./audio"],
        cwd=workdir,
    )
    print(proc.stdout or "", flush=True)
    if proc.returncode != 0:
        log("⚠ 音频校验有警告（不阻断流程）")


def step_script(workdir, timeout):
    section("步骤 6/8 · 生成 Manim 动画代码（LLM）")
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
    section("步骤 7/8 · 代码结构检查")
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
    section(f"步骤 8/8 · 渲染视频（质量 -q{quality}）")

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
    parser.add_argument("-g", "--grade", default="不限",
                        help="按哪个年级的知识点讲解，如「初二上」「八上」「初二」「小学五年级」；"
                             "默认 不限（不约束解法）")
    parser.add_argument("--kb-dir", default=None,
                        help="教材知识点笔记目录（按册存放的 Markdown）；"
                             f"默认读环境变量 {KB_DIR_ENV}，再退回内置路径")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="渲染失败重试次数，默认 3")
    parser.add_argument("--max-audit-retries", type=int, default=2,
                        help="范围审计不通过时的分镜重做次数，默认 2")
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

    # 先解析知识库，好在启动横幅里如实显示本次的范围约束
    kb_path = Path(args.kb_dir).resolve() if args.kb_dir else resolve_kb_dir()
    index = load_kb_index(kb_path) if kb_path else {}
    volumes = resolve_volumes(args.grade) if index else None
    kb_scope = build_scope_text(index, volumes) if volumes else None

    print(f"\n{'=' * 66}")
    print("  MathLens 端到端流水线")
    print(f"{'=' * 66}")
    print(f"  题目图片: {image}")
    print(f"  输出目录: {workdir}")
    print(f"  渲染质量: {args.quality}")
    band = resolve_grade(args.grade)
    print(f"  配音音色: {args.voice}")
    print(f"  学情年级: {args.grade}" + (f"（{band}知识范围）" if band else ""))
    if kb_scope:
        print(f"  知识库:   {kb_path}")
        print(f"  范围约束: {'、'.join(volumes)}"
              f"（{len(volumes)} 册 / {len(kb_scope)} 字，含解题后审计）")
    elif kb_path and index:
        print(f"  知识库:   {kb_path}"
              f"（{len(index)} 册，但「{args.grade}」映射不到册别，本次未启用）")
    elif kb_path:
        print(f"  知识库:   {kb_path}（未找到可识别的教材笔记）")
    else:
        print("  知识库:   未配置（仅按学段约束解法）")

    total_start = time.time()

    setup_ffmpeg()
    step_prepare(workdir)
    step_storyboard_guarded(image, workdir, args.llm_timeout, args.grade,
                            index, volumes, args.max_audit_retries)
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
