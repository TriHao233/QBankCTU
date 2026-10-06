import re
from typing import List, Dict, Any

# Tập hợp các ký tự tiếng Việt hợp lệ
VIETNAMESE_CHARS = set(
    "àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡ"
    "ùúụủũưừứựửữỳýỵỷỹđ"
    "ÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠ"
    "ÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ"
)

# Pattern nhận diện dòng code C (để tránh bị xóa nhầm)
CODE_PATTERNS = [
    r'^\s*#\s*include\b',
    r'^\s*#\s*define\b',
    r'\b(printf|scanf|main|int|float|char|void|return|for|while|if|else|switch|case|break|typedef|struct)\s*[\(\{;]',
    r'^\s*\{', r'^\s*\}', r';\s*$',
    r'^\s*(int|float|char|double|long|unsigned|void|short)\s+\w+',
    r'\b(fopen|fclose|fread|fwrite|fprintf|fscanf|gets|puts|getch)\s*\(',
    r'^\s*\/\*', r'^\s*\*\/', r'^\s*\/\/',
]

SEPARATOR_PATTERN = re.compile(r'^[\s\-—–=_\.~*#@]{3,}$')
DOTS_PATTERN = re.compile(r'\.{4,}')
NUMBER_NOISE_PATTERN = re.compile(r'[0-9]{8,}')
SINGLE_CHAR_LINE = re.compile(r'^\s*[^\w\s]\s*$')
BRACKET_NOISE_PATTERN = re.compile(r'(\[.{0,5}\]\s*){4,}')
REPEATING_SHORT_PATTERN = re.compile(r'(.)\1{3,}|(.{2,3})\2{2,}')

GARBAGE_SEQUENCES = [
    '**', '##', '@@', '~~', '``', 'ốôốôốẽốẽ', 'ố7ố7ố7', 'ẽẽ', 'ốc',
    'e6e6nnnrnrnẽ', 'eưểvwwwxvr', 'xcccevv', 'CỐ ố7ố7ố7ố7ố7ẽẽ', 'LNÚh', 'GHẾ', 'ốốốố',
]

def is_code_line(line: str) -> bool:
    """Kiểm tra cực kỳ khắt khe để không bắt nhầm câu văn xuôi thành Code"""
    stripped = line.strip()
    if not stripped: return False

    # 1. Các dấu hiệu chắc chắn 100% là dòng code (Không thể lẫn vào văn xuôi)
    strong_patterns = [
        r"^#include", r"^#define",         # Thư viện, macro
        r"^int main", r"^void main",       # Hàm main
        r"^void \w+",                      # Khai báo hàm
        r"^(printf|scanf|gets|puts)\(",    # Hàm I/O chuẩn
        r"^(if|while|for|switch)\s*\(",    # Vòng lặp, rẽ nhánh
        r"^typedef struct", r"^struct \w+",# Cấu trúc
        r"\}$", r"^\{$"                    # Đóng/mở ngoặc nhọn
    ]
    for p in strong_patterns:
        if re.search(p, stripped):
            return True

    # 2. Dấu hiệu yếu hơn (Cần kết hợp điều kiện)
    # Nếu dòng kết thúc bằng dấu chấm phẩy (;) HOẶC có dấu ngoặc () / ngoặc vuông []
    if stripped.endswith(";") or ("(" in stripped and ")" in stripped) or ("[" in stripped and "]" in stripped):
        # Và phải chứa từ khóa lập trình đặc trưng hoặc phép gán
        code_keywords = [
            "int ", "float ", "char ", "double ", "long ",
            "return ", "break", "continue", "sizeof", "="
        ]
        if any(kw in stripped for kw in code_keywords):
            # Lọc bỏ trường hợp ví dụ trong văn xuôi (Ví dụ: "(x xem như là biến int);")
            if "Ví dụ" not in stripped and "Thí dụ" not in stripped:
                return True

    return False

def calculate_meaningful_ratio(line: str) -> float:
    if not line.strip(): return 1.0
    total = len(line.strip())
    meaningful = sum(1 for ch in line.strip() if ch.isalnum() or ch.isspace() or ch in VIETNAMESE_CHARS)
    meaningful += sum(0.5 for ch in line.strip() if ch in '.,;:!?-()[]{}\'"/\\+*=%<>@#$&_~`^|')
    return meaningful / total if total > 0 else 1.0

def has_formula_marker(line: str) -> bool:
    stripped = line.strip()
    return (stripped.startswith('$$') and stripped.endswith('$$')) or '<FORMULA_BLOCK' in stripped or '</FORMULA_BLOCK>' in stripped

def is_garbled_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or has_formula_marker(stripped) or is_code_line(stripped): return False
    if re.match(r'^\s*(?:[IVXLC]+\.|\d+\.|[A-ZĐ]\.)\s+', stripped) or re.match(r'^\s*(?:Chương|Bài|Thí dụ|Ví dụ|Mục|Phần)\b', stripped, re.IGNORECASE): return False
    if re.search(r'[=+\-*/^√]', stripped) and re.search(r'\d', stripped) and re.search(r'[A-Za-zÀ-ỹ]', stripped): return False
    if SEPARATOR_PATTERN.match(stripped): return True

    repeat_matches = list(REPEATING_SHORT_PATTERN.finditer(stripped))
    if repeat_matches and sum(m.end() - m.start() for m in repeat_matches) > len(stripped) * 0.3: return True
    if any(seq in stripped and len(seq) > 2 for seq in GARBAGE_SEQUENCES): return True

    text_without_brackets = re.sub(r'\[.*?\]', '', stripped).strip()
    if BRACKET_NOISE_PATTERN.search(stripped) and len(text_without_brackets) < 10: return True

    meaningful_words = re.findall(r'[A-Za-zÀ-ỹ]{2,}', stripped)
    if len(meaningful_words) >= 5 and calculate_meaningful_ratio(stripped) >= 0.40: return False
    if calculate_meaningful_ratio(stripped) < 0.35 and len(stripped) > 5: return True

    words = stripped.split()
    if len(words) > 3 and sum(1 for w in words if len(w) <= 2) > len(words) * 0.5: return True
    if len(words) > 5 and (sum(len(w) for w in words) / len(words)) < 3.0: return True

    triple_repeat_count = len(re.findall(r'(.)\1{2,}', stripped))
    if (triple_repeat_count >= 2 and len(stripped) < 60) or (triple_repeat_count >= 1 and len(stripped) < 15): return True

    text_without_numbers = re.sub(r'[0-9]+', '', stripped).strip()
    if NUMBER_NOISE_PATTERN.search(stripped) and len(text_without_numbers) < len(stripped) * 0.3: return True

    special_count = sum(1 for c in stripped if not c.isalnum() and not c.isspace())
    if len(stripped) > 5 and special_count > len(stripped) * 0.5: return True

    if len(stripped) < 25 and len(words) >= 2:
        real_words = [w for w in words if len(w) >= 4 and w.isalpha()]
        if len(real_words) < len(words) * 0.25 and len(stripped) < 20: return True

    return not (len(meaningful_words) >= 4 and calculate_meaningful_ratio(stripped) >= 0.45)

def clean_line(line: str) -> str:
    text = re.sub(r'\*\*+|###+|@@+|~~+', '', line)
    text = DOTS_PATTERN.sub('...', text)
    text = re.sub(r'[—–]{3,}|-{4,}', '', text)
    return re.sub(r'[ \t]+', ' ', text).strip()

def fix_exponents(text: str) -> str:
    if not text: return text
    text = re.sub(r'(\d+\.?\d*)\s*\*\s*10(\d{2,})', r'\1 * 10^\2', text)
    text = re.sub(r'\b10(\d{3,})\b', r'10^\1', text)
    text = re.sub(r'\b10\s*([+-])(\d{1,4})\b', r'10^\1\2', text)
    text = re.sub(r'\(-1\)["\'](\w)', r'(-1)^\1', text)
    text = re.sub(r'(\w)\*2([A-Z])\b', r'\1*2^\2', text)
    text = re.sub(r'(\w)/2([A-Z])\b', r'\1/2^\2', text)
    return re.sub(r'(\d)\*(\d)(\d)\)', r'\1*\2^\3)', text)

def fix_ocr_typos(line: str) -> str:
    """Sửa các lỗi rớt ký tự hoặc dính chữ thường gặp của OCR"""
    stripped = line.strip()
    if not stripped: return line

    # Khôi phục dấu # cho include và define (rất hay bị rớt)
    if stripped.startswith("include <") or stripped.startswith("include<") or stripped.startswith("include "):
        line = line.replace("include", "#include", 1)
    elif stripped.startswith("define "):
        line = line.replace("define", "#define", 1)

    # Tách chữ hoa bị dính liền với chữ thường (VD: TrongC -> Trong C)
    line = re.sub(r'([a-zàáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ])([A-Z])', r'\1 \2', line)

    return line

def clean_page_text(text: str) -> str:
    if not text or not text.strip(): return text
    lines = text.split('\n')

    cleaned_lines = []
    for l in lines:
        fixed_line = fix_ocr_typos(l)
        cleaned = clean_line(fixed_line)
        if not is_garbled_line(cleaned) or not cleaned.strip():
            cleaned_lines.append(cleaned)

    final_lines = []
    for line in cleaned_lines:
        stripped = line.strip()
        if not stripped or has_formula_marker(stripped) or is_code_line(stripped):
            final_lines.append(line)
            continue
        if len(stripped) <= 2 and not stripped.isdigit() and stripped not in ['e', 'o', '-'] and not re.match(r'^[\d•\-]$', stripped): continue
        if SINGLE_CHAR_LINE.match(stripped) or (0 < len(stripped) < 5 and not re.match(r'^[•\-\d\w][\.:\)]?\s*$', stripped) and stripped not in ['+', '-', '*', '/', '=', '<', '>', '>=', '<=', '!=']): continue
        final_lines.append(line)

    result_lines, blank_count = [], 0
    for line in final_lines:
        if not line.strip():
            blank_count += 1
            if blank_count <= 2: result_lines.append('')
        else:
            blank_count = 0
            result_lines.append(line)

    # --- TÍCH HỢP CODE BLOCK Ở ĐÂY ---
    # Chạy kết quả qua bộ gom code block (mặc định set ngôn ngữ là cpp)
    formatted_lines = format_code_blocks(result_lines, default_lang="cpp")

    # Gọi xử lý công thức mũ và ghép dòng
    return fix_exponents('\n'.join(formatted_lines)).strip()

def format_code_blocks(lines: list[str], default_lang: str = "cpp") -> list[str]:
    """
    Quét qua các dòng, gom các dòng code liên tiếp nhau và bọc trong Markdown Code Block.
    Giữ lại khoảng trắng (dòng trống) nếu nó nằm giữa đoạn code.
    """
    result = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()

        # Xử lý dòng trống
        if not stripped:
            if in_code_block:
                result.append(line)  # Giữ lại dòng trống nằm giữa code
            else:
                result.append(line)
            continue

        # Kiểm tra xem dòng hiện tại có phải là code không
        if is_code_line(stripped):
            if not in_code_block:
                # Mở block code
                result.append(f"\n```{default_lang}")
                in_code_block = True

            # Giữ nguyên dòng gốc (không dùng stripped) để cố gắng bảo toàn thụt lề nếu có
            result.append(line)
        else:
            if in_code_block:
                # Đóng block code
                result.append("```\n")
                in_code_block = False
            result.append(line)

    # Đóng block code nếu tài liệu kết thúc ngay lúc đoạn code đang mở
    if in_code_block:
        result.append("```\n")

    return result

def clean_ocr_pages(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"page_number": p["page_number"], "text": clean_page_text(p.get("text", ""))} for p in pages]
