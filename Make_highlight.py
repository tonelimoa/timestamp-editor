"""
サッカーハイライト動画自動生成スクリプト
使い方: python make_highlight.py
"""

import subprocess
import os
import sys
import tempfile
import shutil
import unicodedata
from datetime import datetime

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Pillowが必要です。以下を実行してください:")
    print("  pip install Pillow")
    sys.exit(1)


# ===== 設定 =====
VIDEOS_DIR = "videos"
OUTPUT_DIR = "output"
BGM_FILE   = "bgm.mp3"
TIMESTAMPS_FILE = "timestamps.txt"  # デフォルト（後方互換用）
BGM_VOLUME = 0.5
FADE_DURATION = 0.5

# テロップサイズの基準解像度（この解像度を基準に文字・余白等を比例スケーリング）
# テロップサイズの基準解像度（この解像度を基準に文字・余白等を比例スケーリング）
TELOP_BASE_WIDTH = 1280
TELOP_BASE_HEIGHT = 720

# 出力統一仕様（解像度・音声仕様が異なる動画が混在してもここに揃える。結合時のカクつき・音ズレ防止）
OUTPUT_WIDTH = 1280
OUTPUT_HEIGHT = 720
OUTPUT_SAMPLE_RATE = 44100
OUTPUT_CHANNELS = 1

# GPUエンコード（NVENC）を使うか。Noneなら起動時に自動判定。
USE_NVENC = None

# フォント候補（上から順に試す）
# 注意: MS Gothicは小サイズでビットマップ化してピクセル表示になるため後回し
# Bold版を優先して太字表示にする
FONT_CANDIDATES = [
    "C:/Windows/Fonts/YuGothB.ttc",   # 游ゴシック Bold
    "C:/Windows/Fonts/meiryob.ttc",   # メイリオ Bold
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    "C:/Windows/Fonts/msmincho.ttc",
]


def find_font():
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


_videos_dir_cache = None  # {正規化(NFC)したファイル名: 実際のファイル名} のキャッシュ


def resolve_video_path(filename: str):
    """videosフォルダ内から、Unicode正規化(NFC/NFD)の違いや前後の空白の違いを
    吸収した上でファイルを探す。見つかった実際のパスを返す（無ければNone）。

    背景: Mac/ブラウザ経由の入力とWindows上の実ファイルとで、同じ「ズ」のような
    濁点文字でも内部表現が正規化形式(NFC)/分解形式(NFD)で異なる場合があり、
    単純な文字列比較・os.path.existsでは一致しないことがある。"""
    global _videos_dir_cache

    # 1. まずそのまま試す（従来通りの最速パス）
    direct_path = os.path.join(VIDEOS_DIR, filename)
    if os.path.exists(direct_path):
        return direct_path

    if not os.path.isdir(VIDEOS_DIR):
        return None

    # 2. キャッシュがなければvideosフォルダの中身を正規化して索引化
    if _videos_dir_cache is None:
        _videos_dir_cache = {}
        for actual in os.listdir(VIDEOS_DIR):
            key = unicodedata.normalize("NFC", actual).strip()
            _videos_dir_cache[key] = actual

    # 3. 正規化・空白除去したファイル名で再検索
    normalized_target = unicodedata.normalize("NFC", filename).strip()
    actual = _videos_dir_cache.get(normalized_target)
    if actual:
        resolved_path = os.path.join(VIDEOS_DIR, actual)
        if actual != filename:
            print(f"  [情報] ファイル名の表記ゆれを自動補正: \"{filename}\" → \"{actual}\"")
        return resolved_path

    return None


def check_nvenc_available() -> bool:
    """h264_nvencが実際に使えるか試しエンコードして確認する"""
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15
        )
        return result.returncode == 0
    except Exception:
        return False


def run_cmd(cmd: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace"
    )


def time_to_seconds(t: str) -> float:
    parts = t.strip().split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"時間形式が不正です: {t}")


def get_duration(filepath: str) -> float:
    result = run_cmd(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", filepath]
    )
    return float(result.stdout.strip())


def get_video_size(filepath: str):
    result = run_cmd([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        filepath
    ])
    w, h = result.stdout.strip().split(",")
    return int(w), int(h)


def make_telop_image(tmp_dir: str, index: int, line1: str, line2: str, width: int, height: int, font_path: str) -> str:
    """テロップ用PNG画像を生成（スポーツ中継風デザイン）
    動画の解像度（基準: 1280x720）に比例して文字サイズ・余白等をスケーリングする"""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 基準解像度に対する拡大率（縦横どちらか小さい方の比率を採用し、極端な縦長/横長でも崩れにくくする）
    scale = min(width / TELOP_BASE_WIDTH, height / TELOP_BASE_HEIGHT)

    def s(value):
        """基準値をscale倍して整数化（最小1px）"""
        return max(1, round(value * scale))

    size1, size2 = s(20), s(23)
    try:
        font1 = ImageFont.truetype(font_path, size1)
        font2 = ImageFont.truetype(font_path, size2)
    except Exception:
        font1 = ImageFont.load_default()
        font2 = ImageFont.load_default()

    pad_x, pad_y = s(18), s(8)
    line_gap = s(7)
    margin = s(20)
    cut = s(16)            # 右下コーナーカットのサイズ
    accent_w = s(5)         # 左端アクセントバーの幅
    text_offset = accent_w + s(6)  # アクセントバーからテキストまでの距離

    bbox1 = draw.textbbox((0, 0), line1, font=font1)
    bbox2 = draw.textbbox((0, 0), line2, font=font2)
    w1, h1 = bbox1[2] - bbox1[0], bbox1[3] - bbox1[1]
    w2, h2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]

    box_w = max(w1, w2) + pad_x * 2 + text_offset
    box_h = h1 + h2 + line_gap + pad_y * 2

    box_x = margin
    box_y = height - box_h - margin

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)

    # 右下を斜めにカットした多角形（スポーツ中継風）
    points = [
        (box_x, box_y),
        (box_x + box_w, box_y),
        (box_x + box_w, box_y + box_h - cut),
        (box_x + box_w - cut, box_y + box_h),
        (box_x, box_y + box_h),
    ]
    ov_draw.polygon(points, fill=(10, 20, 40, 200))

    # 左端アクセントバー（ブルー）
    ov_draw.rectangle(
        [box_x, box_y, box_x + accent_w, box_y + box_h],
        fill=(33, 150, 243, 255)
    )

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    tx = box_x + pad_x + text_offset
    ty1 = box_y + pad_y
    ty2 = ty1 + h1 + line_gap

    # 大会名・日付: ライトグレー
    draw.text((tx, ty1), line1, font=font1, fill=(200, 210, 230, 255))
    # 選手名: 白
    draw.text((tx, ty2), line2, font=font2, fill=(255, 255, 255, 255))

    out_path = os.path.join(tmp_dir, f"telop_{index:03d}.png")
    img.save(out_path, "PNG")
    return out_path


def make_clip(row: dict, tmp_dir: str, index: int, font_path: str, use_nvenc: bool = False):
    video_path = resolve_video_path(row["filename"])
    if not video_path:
        print(f"  [スキップ] ファイルが見つかりません: {os.path.join(VIDEOS_DIR, row['filename'])}")
        return None

    try:
        center_sec = time_to_seconds(row["timestamp"])
        pre_sec    = float(row["pre_sec"])
        post_sec   = float(row["post_sec"])
    except ValueError as e:
        print(f"  [スキップ] {e}")
        return None

    duration_total = get_duration(video_path)
    start = max(0, center_sec - pre_sec)
    end   = min(duration_total, center_sec + post_sec)
    clip_duration = end - start

    if clip_duration <= 0:
        print(f"  [スキップ] タイムスタンプが動画範囲外: {row['filename']} {row['timestamp']}")
        return None

    if row.get('opponent'):
        line1 = f"{row['event']}  {row['date']}  VS {row['opponent']}"
    else:
        line1 = f"{row['event']}  {row['date']}"
    line2 = f"{row['player']}  Good play !"
    fade_out_start = max(0, clip_duration - FADE_DURATION)

    out_path = os.path.join(tmp_dir, f"clip_{index:03d}.mp4")

    # テロップ画像は出力統一解像度を基準に作成（元動画の解像度がバラバラでも文字サイズが揃う）
    telop_path = make_telop_image(tmp_dir, index, line1, line2, OUTPUT_WIDTH, OUTPUT_HEIGHT, font_path)

    # 切り出し＋スケール統一＋フェード＋テロップ合成を1回のエンコードに統合（高速化のポイント）
    # scale + setsar: 解像度をOUTPUT_WIDTH/HEIGHTへ強制統一（異なる解像度の素材が混在しても結合できるようにする）
    # fps=30 + -vsync cfr: iPhoneのVFR（可変フレームレート）をCFR（固定30fps）に変換してカクつき防止
    # -ar/-ac: 音声サンプルレート・チャンネル数を統一（不一致による音ズレ・結合エラー防止）
    if use_nvenc:
        # GPU(NVENC)エンコード: GTX1650等のTuring世代以降で高速。スマホ視聴用途には十分な画質
        video_codec_opts = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23", "-b:v", "0"]
    else:
        # CPU(libx264)エンコード: veryfast+crf23でスマホ視聴用途として十分な速度・画質バランス
        video_codec_opts = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]

    r1 = run_cmd([
        "ffmpeg", "-y",
        "-ss", str(start),
        "-t", str(clip_duration),
        "-i", video_path,
        "-i", telop_path,
        "-filter_complex",
        (
            f"[0:v]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"fps=30,"
            f"fade=t=in:st=0:d={FADE_DURATION},"
            f"fade=t=out:st={fade_out_start}:d={FADE_DURATION}[v];"
            f"[v][1:v]overlay=0:0[vout]"
        ),
        "-af", f"afade=t=in:st=0:d={FADE_DURATION},afade=t=out:st={fade_out_start}:d={FADE_DURATION}",
        "-map", "[vout]", "-map", "0:a",
        *video_codec_opts,
        "-c:a", "aac", "-ar", str(OUTPUT_SAMPLE_RATE), "-ac", str(OUTPUT_CHANNELS),
        "-vsync", "cfr",
        out_path
    ])
    if r1.returncode != 0:
        print(f"  [エラー] クリップ生成失敗")
        print(r1.stderr[-400:])
        return None

    try:
        os.remove(telop_path)
    except:
        pass

    print(f"  [OK] {row['filename']} {row['timestamp']} → {os.path.basename(out_path)}")
    return out_path


def concat_clips(clip_paths: list, tmp_dir: str) -> str:
    list_file = os.path.join(tmp_dir, "concat_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")

    concat_path = os.path.join(tmp_dir, "concat.mp4")
    result = run_cmd([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        concat_path
    ])
    if result.returncode != 0:
        print("[エラー] クリップ結合失敗")
        print(result.stderr[-800:])
        sys.exit(1)
    return concat_path


def add_bgm(video_path: str, output_path: str):
    if not os.path.exists(BGM_FILE):
        print(f"  [情報] {BGM_FILE} が見つかりません。BGMなしで出力します。")
        shutil.copy(video_path, output_path)
        return

    video_duration = get_duration(video_path)

    result = run_cmd([
        "ffmpeg", "-y",
        "-i", video_path,
        "-stream_loop", "-1", "-i", BGM_FILE,
        "-filter_complex",
        (
            f"[1:a]volume={BGM_VOLUME},afade=t=out:st={max(0, video_duration-2)}:d=2[bgm];"
            f"[0:a][bgm]amix=inputs=2:duration=first[aout]"
        ),
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-t", str(video_duration),
        output_path
    ])
    if result.returncode != 0:
        print("[エラー] BGM追加失敗")
        print(result.stderr[-800:])
        sys.exit(1)


def decode_kv_value(value: str) -> str:
    """エスケープされた値（\\n, \\,, \\\\）を元の文字列に戻す"""
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt == "n":
                out.append("\n"); i += 2; continue
            if nxt in (",", "\\"):
                out.append(nxt); i += 2; continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_kv_line(line: str) -> dict:
    """"file=a.mp4, ts=00:12, comment=x\\,y" のような1行をdictにパースする。
    timestamp_editor.html側のencodeKV/parseKVLineと対になる実装。
    エスケープされていないカンマのみを区切り文字として扱う。"""
    obj = {}
    buf = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            buf += ch + line[i + 1]
            i += 2
            continue
        if ch == ",":
            if "=" in buf:
                key, _, val = buf.partition("=")
                obj[key.strip()] = decode_kv_value(val.strip())
            buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    if "=" in buf:
        key, _, val = buf.partition("=")
        obj[key.strip()] = decode_kv_value(val.strip())
    return obj


def find_timestamp_files():
    """カレントディレクトリからtimestampsファイルを探す。
    timestamps.txt / timestamps_01.txt / timestamps_02.txt ... の順で収集。"""
    files = []
    # 番号付きファイルを優先収集
    for f in sorted(os.listdir(".")):
        if f.startswith("timestamps_") and f.endswith(".txt"):
            files.append(f)
    # 番号なし（timestamps.txt）も存在すれば追加
    if os.path.exists("timestamps.txt"):
        files.insert(0, "timestamps.txt")
    return files


def select_files_interactive(files):
    """複数ファイルが見つかった場合に対話的に処理対象を選ばせる。"""
    print("\n以下のタイムスタンプファイルが見つかりました：")
    for i, f in enumerate(files, 1):
        print(f"  {i}: {f}")
    print()
    print("処理するファイルを選択してください。")
    nums = [str(i) for i in range(1, len(files) + 1)]
    print(f"  {' / '.join(nums)}: 番号で1つ選択")
    print(f"  all: すべて処理")
    print()

    while True:
        choice = input("選択してください: ").strip().lower()
        if choice == "all":
            return files
        if choice in nums:
            return [files[int(choice) - 1]]
        print(f"  ※ {' または '.join(nums + ['all'])} を入力してください。")


def process_one_file(timestamps_file, font_path, use_nvenc, suffix=""):
    """1つのタイムスタンプファイルを処理してハイライト動画を生成する。"""
    rows = []
    with open(timestamps_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            obj = parse_kv_line(line)
            filename = obj.get("file", "")
            timestamp = obj.get("ts", "")
            if not filename or not timestamp:
                print(f"  [スキップ] file/tsが読み取れません: {line}")
                continue
            rows.append({
                "filename":  filename,
                "timestamp": timestamp,
                "pre_sec":   obj.get("pre", "7"),
                "post_sec":  obj.get("post", "3"),
                "event":     obj.get("event", ""),
                "opponent":  obj.get("opponent", ""),
                "date":      obj.get("date", ""),
                "player":    obj.get("player", ""),
            })

    if not rows:
        print(f"  [スキップ] {timestamps_file} に処理対象がありません。")
        return None

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tmp_dir = tempfile.mkdtemp()

    try:
        print(f"\n=== クリップ生成開始（{len(rows)}件）: {timestamps_file} ===")
        clip_paths = []
        skip_count = 0

        for i, row in enumerate(rows):
            path = make_clip(row, tmp_dir, i, font_path, use_nvenc=use_nvenc)
            if path:
                clip_paths.append(path)
            else:
                skip_count += 1

        if not clip_paths:
            print("  生成できたクリップが0件でした。")
            return None

        print(f"\n=== クリップ結合中（{len(clip_paths)}件）===")
        concat_path = concat_clips(clip_paths, tmp_dir)

        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        label = f"_{suffix}" if suffix else ""
        final_output = os.path.join(OUTPUT_DIR, f"highlight{label}_{timestamp_str}.mp4")

        print(f"\n=== BGM追加中 ===")
        add_bgm(concat_path, final_output)

        print(f"\n=== 完了: {final_output} ===")
        print(f"成功: {len(clip_paths)}件 / スキップ: {skip_count}件")
        return final_output

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    # フォント確認
    font_path = find_font()
    if font_path:
        print(f"フォント: {font_path}")
    else:
        print("[警告] 日本語フォントが見つかりません。テロップが文字化けする可能性があります。")
        font_path = ""

    # NVENC(GPUエンコード)が使えるか判定
    if USE_NVENC is None:
        print("GPUエンコード(NVENC)の利用可否を確認中...")
        use_nvenc = check_nvenc_available()
    else:
        use_nvenc = USE_NVENC
    print(f"エンコーダ: {'GPU (h264_nvenc)' if use_nvenc else 'CPU (libx264)'}")

    # タイムスタンプファイルを探す
    all_files = find_timestamp_files()

    if not all_files:
        print("エラー: timestamps.txt または timestamps_01.txt などが見つかりません。")
        sys.exit(1)

    # 1ファイルだけなら自動処理、複数なら選択させる
    if len(all_files) == 1:
        target_files = all_files
        print(f"タイムスタンプファイル: {all_files[0]}")
    else:
        target_files = select_files_interactive(all_files)

    # 選択されたファイルを順番に処理
    results = []
    for f in target_files:
        # suffixはファイル名から "_01" 部分を抽出（なければ空）
        base = os.path.splitext(f)[0]  # "timestamps_01"
        suffix = base[len("timestamps"):].lstrip("_")  # "01" or ""
        out = process_one_file(f, font_path, use_nvenc, suffix=suffix)
        if out:
            results.append(out)

    print(f"\n{'='*40}")
    print(f"全処理完了: {len(results)}/{len(target_files)} ファイル成功")
    for r in results:
        print(f"  → {r}")


if __name__ == "__main__":
    main()
