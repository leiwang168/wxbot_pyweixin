"""
通用 OCR 工具 — 用 Tesseract 识别图片中的文字
用法: python tesseract_ocr.py <图片路径> inbox目录
"""
import sys
from PIL import Image
import pytesseract

def ocr_image(image_path: str, lang: str = 'chi_sim') -> str:
    """识别图片文字，默认中文"""
    img = Image.open(image_path)
    text = pytesseract.image_to_string(img, lang=lang)
    return text.strip()

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: python tesseract_ocr.py <图片路径>')
        sys.exit(1)
    result = ocr_image(sys.argv[1])
    print(result)
