"""
通用 OCR 工具 — 用 Tesseract 识别图片中的文字
用法: python tesseract_ocr.py <图片路径> inbox目录
"""
import sys
import os
from PIL import Image
import pytesseract

# 设置独立安装的 tesseract 二进制和 tessdata
TESSERACT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python_deps', 'tesseract')
TESSERACT_BIN = os.path.join(TESSERACT_DIR, 'bin', 'tesseract')
TESSDATA_DIR = os.path.join(TESSERACT_DIR, 'tessdata')
LIB_DIR = os.path.join(TESSERACT_DIR, 'lib')

if os.path.exists(TESSERACT_BIN):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_BIN
    os.environ['LD_LIBRARY_PATH'] = f"{LIB_DIR}:{os.environ.get('LD_LIBRARY_PATH', '')}"
    os.environ['TESSDATA_PREFIX'] = TESSDATA_DIR

def ocr_image(image_path: str, lang: str = 'chi_sim') -> str:
    """识别图片文字，默认中文"""
    img = Image.open(image_path)
    text = pytesseract.image_to_string(img, lang=lang)
    return text.strip()

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: python tesseract_ocr.py <图片路径>')
        sys.exit(1)
    result = ocr_image(sys.argv[1], lang=sys.argv[2] if len(sys.argv) > 2 else 'chi_sim+eng')
    print(result)
