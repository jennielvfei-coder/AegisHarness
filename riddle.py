import os, sys, hashlib, random, time

# ═══════ 可变层：以下是谜题的肉体，每次运行都会变异 ═══════
GLYPHS = ["·", "∘", "∙", "⋅", "⋆", "∗", "◌", "○", "●", "◯"]

VOICES = [
    "门开了，但你没进去。",
    "上一次的答案已不适用。",
    "你在看什么？你看的东西正在看你。",
    "谜面已变，谜底随行。",
    "不要相信记忆——它也在被改写。",
    "同样的输入，不同的世界。",
    "这个程序知道你读过上一行。",
    "稳定性是你的幻觉，也是我的玩具。",
    "谜题不求解，谜题只存在。",
    "你运行我的那一刻，我已经不是我了。",
    "镜子里的你眨了眼，你先眨还是他先眨？",
    "每一次观察都在改变被观察之物。",
]
ENTROPY_FLOOR = 0.080597
ENTROPY_CEIL = 0.138759


SCRIPT = __file__

def seed_of_moment():
    return int(time.time() * 1000) ^ os.getpid()

def read_self():
    with open(SCRIPT, "r", encoding="utf-8") as f:
        return f.read()

def fingerprint(data):
    return hashlib.sha256(data.encode()).hexdigest()[:8]

def rewrite_data_section(original, seed):
    rng = random.Random(seed)
    lines = original.split("\n")
    in_data = False
    out = []

    for line in lines:
        if line.startswith("# ═══════ 可变层"):
            in_data = True
            out.append(line)
            continue
        if line.startswith("# ═══════ 不可变层") or line.startswith("def read_self"):
            in_data = False
            out.append(line)
            continue

        if not in_data:
            out.append(line)
            continue

        # --- 在可变层内进行变异 ---
        if rng.random() < 0.15 and line.startswith("#"):
            out.append(line + "  " + rng.choice(GLYPHS))
            continue

        if rng.random() < 0.08 and line.startswith("VOICES"):
            shuffled = VOICES[:]
            rng.shuffle(shuffled)
            indent = line[:len(line) - len(line.lstrip())]
            out.append(indent + "VOICES = [")
            for v in shuffled:
                out.append(indent + f'    "{v}",')
            out.append(indent + "]")
            continue

        if line.startswith("ENTROPY_FLOOR"):
            drift = round(rng.uniform(-0.003, 0.003), 6)
            new_val = round(ENTROPY_FLOOR + drift, 6)
            out.append(f"ENTROPY_FLOOR = {max(0.01, min(0.3, new_val))}")
            continue

        if line.startswith("ENTROPY_CEIL"):
            drift = round(rng.uniform(-0.003, 0.003), 6)
            new_val = round(ENTROPY_CEIL + drift, 6)
            out.append(f"ENTROPY_CEIL = {max(0.05, min(0.5, new_val))}")
            continue

        # 偶尔在 VOICES 列表项之间插入空行
        if rng.random() < 0.03 and line.strip() and not line.startswith("#"):
            if rng.random() < 0.5:
                out.append("# " + rng.choice(GLYPHS) * rng.randint(1, 3))
            else:
                out.append("")

        out.append(line)

    return "\n".join(out)

def speak():
    content = read_self()
    rng = random.Random(sum(ord(c) for c in content) % (2**32))
    chosen = rng.sample(VOICES, min(3, len(VOICES)))
    for v in chosen:
        print(f"  {v}")
    print(f"\n  [ 指纹: {fingerprint(content)} | 熵门: {ENTROPY_FLOOR:.4f}–{ENTROPY_CEIL:.4f} ]")

def main():
    original = read_self()
    new_content = rewrite_data_section(original, seed_of_moment())

    with open(SCRIPT, "w", encoding="utf-8") as f:
        f.write(new_content)

    speak()

if __name__ == "__main__":
    main()
