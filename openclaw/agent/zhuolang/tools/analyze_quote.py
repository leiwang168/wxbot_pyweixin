"""
通用 Excel 报价单解析工具
用法: python analyze_quote.py <xlsx文件路径> [--json] [--csv]
输出: 自动识别列名、提取产品行、计算价格区间
"""
import sys
import os
import json
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("[错误] 需要安装 pandas: pip install pandas openpyxl")
    sys.exit(1)


def load_excel(filepath: str) -> pd.DataFrame:
    """加载 Excel 文件，自动跳过空行和合并单元格"""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"文件不存在: {filepath}")

    # 尝试读取，pandas 会自动处理大部分格式
    try:
        df = pd.read_excel(filepath, header=None, dtype=str)
    except Exception:
        # 尝试指定 sheet
        xl = pd.ExcelFile(filepath)
        df = pd.read_excel(filepath, sheet_name=xl.sheet_names[0], header=None, dtype=str)

    # 清理全空行和全空列
    df = df.dropna(how='all').dropna(axis=1, how='all')
    df = df.fillna('')
    return df


def find_header_row(df: pd.DataFrame) -> int:
    """自动定位表头行：找包含≥3个中文关键词的行"""
    keywords = ['价', '品名', '名称', '规格', '型号', '数量', '单位', '单价', '交期',
                '品牌', '产品', 'item', 'price', 'name', '产品名称', '价格', '元']
    best_row, best_score = 0, 0
    for i, row in df.iterrows():
        score = sum(1 for v in row if isinstance(v, str) and any(k in v for k in keywords))
        if score > best_score:
            best_row, best_score = i, score
    return best_row if best_score >= 2 else 0


def find_data_rows(df: pd.DataFrame, header_row: int) -> pd.DataFrame:
    """定位数据行：表头之后、第一个全空行之前"""
    data = df.iloc[header_row + 1:]
    # 找终止行（全空或包含"合计""总计"）
    stop = len(data)
    for i, row in data.iterrows():
        row_str = ' '.join(str(v) for v in row if v)
        if row_str.strip() == '':
            stop = i
            break
        if any(k in row_str for k in ['合计', '总计', '备注', '注：', '说明：']):
            stop = i
            break
    return data.head(stop - min(data.index) if isinstance(data.index[0], int) else stop)


def extract_products(df: pd.DataFrame) -> list[dict]:
    """从 DataFrame 提取产品/报价列表"""
    header_row = find_header_row(df)
    headers = [str(v).strip() if v else f'col_{i}' for i, v in enumerate(df.iloc[header_row])]
    data = find_data_rows(df, header_row)

    products = []
    for _, row in data.iterrows():
        row_dict = {}
        for i, h in enumerate(headers):
            val = str(row.iloc[i]).strip() if i < len(row) and row.iloc[i] else ''
            row_dict[h] = val

        # 跳过多行合并产生的空行（第一列为空且其他列也几乎为空）
        non_empty = sum(1 for v in row_dict.values() if v)
        if non_empty < 2:
            continue

        # 自动识别价格列
        price_cols = [k for k in row_dict if any(w in k for w in ['价', 'price', 'Price'])]
        prices = []
        for pc in price_cols:
            try:
                p = float(str(row_dict[pc]).replace('¥', '').replace('元', '').replace(',', '').strip())
                prices.append(p)
            except (ValueError, TypeError):
                pass

        product = {
            **row_dict,
            '_prices': prices,
            '_min_price': min(prices) if prices else None,
            '_max_price': max(prices) if prices else None,
        }
        products.append(product)

    return products


def analyze(filepath: str) -> dict:
    """完整分析流程，返回字典"""
    df = load_excel(filepath)
    products = extract_products(df)

    all_prices = []
    for p in products:
        all_prices.extend(p['_prices'])

    return {
        'file': os.path.basename(filepath),
        'sheets': 1,
        'rows': len(df),
        'cols': len(df.columns),
        'product_count': len(products),
        'price_range': f"{min(all_prices)} ~ {max(all_prices)}" if all_prices else 'N/A',
        'products': products,
    }


def print_report(result: dict):
    """打印可读报告"""
    print(f"\n{'='*60}")
    print(f"[{result['file']}]")
    print(f"{'='*60}")
    print(f"总行数: {result['rows']} | 总列数: {result['cols']}")
    print(f"提取产品/报价数: {result['product_count']}")
    if result['price_range'] != 'N/A':
        print(f"价格区间: {result['price_range']}")
    print(f"{'='*60}\n")

    if not result['products']:
        print("[!] 未提取到产品数据")
        return

    # 表头
    first = result['products'][0]
    display_keys = [k for k in first if not k.startswith('_')]
    col_widths = {}
    for k in display_keys:
        max_w = max(len(k), max((len(str(p.get(k, ''))) for p in result['products']), default=0))
        col_widths[k] = min(max_w, 30)  # 限制最大宽度

    # 打印表头
    header_line = ' | '.join(f'{k:<{col_widths[k]}}' for k in display_keys)
    sep_line = '-+-'.join('-' * col_widths[k] for k in display_keys)
    print(header_line)
    print(sep_line)

    for p in result['products']:
        line = ' | '.join(f'{str(p.get(k, ""))[:col_widths[k]]:<{col_widths[k]}}' for k in display_keys)
        print(line)


def main():
    if len(sys.argv) < 2:
        print('用法: python analyze_quote.py <xlsx文件路径> [--json] [--csv]')
        print('示例: python tools/analyze_quote.py wechat_files/报价单.xlsx')
        print('      python tools/analyze_quote.py 报价单.xlsx --json')
        sys.exit(1)

    filepath = sys.argv[1]
    use_json = '--json' in sys.argv
    use_csv = '--csv' in sys.argv

    try:
        result = analyze(filepath)
    except Exception as e:
        print(f'[错误] 解析失败: {e}')
        sys.exit(1)

    if use_json:
        # 去掉内部字段
        clean = {
            **result,
            'products': [{k: v for k, v in p.items() if not k.startswith('_')} for p in result['products']]
        }
        print(json.dumps(clean, ensure_ascii=False, indent=2))
    elif use_csv:
        if result['products']:
            keys = [k for k in result['products'][0] if not k.startswith('_')]
            print(','.join(keys))
            for p in result['products']:
                print(','.join(f'"{p.get(k, "")}"' for k in keys))
        else:
            print('(无数据)')
    else:
        print_report(result)


if __name__ == '__main__':
    main()
