"""
上传文件到 MinIO（使用 requests 直接 PUT，绕过签名问题）
用法: python upload_to_minio.py <本地文件路径> [远端文件名]
"""
import sys, os, json
import requests
from pathlib import Path
from _config import load_minio_config


def upload_file(local_path: str, remote_path: str = None, public: bool = True) -> str:
    cfg = load_minio_config()
    local_path = str(local_path)
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f'文件不存在: {local_path}')

    if not remote_path:
        filename = os.path.basename(local_path)
        remote_path = f'询价/{filename}'

    endpoint = cfg['endpoint'].rstrip('/')
    bucket = cfg['bucket-name']
    url = f'{endpoint}/{bucket}/{remote_path}'

    print(f'[上传] {local_path} -> {url}')

    with open(local_path, 'rb') as f:
        data = f.read()

    ext = os.path.splitext(local_path)[1].lower()
    mime_map = {
        '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.xls': 'application/vnd.ms-excel',
        '.pdf': 'application/pdf',
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.txt': 'text/plain',
    }
    content_type = mime_map.get(ext, 'application/octet-stream')

    headers = {
        'Content-Type': content_type,
        'Content-Length': str(len(data)),
    }
    if public:
        headers['x-amz-acl'] = 'public-read'

    resp = requests.put(url, data=data, headers=headers, timeout=60)

    if resp.status_code in (200, 201, 204):
        print(f'[成功] {url}')
        return url
    else:
        raise RuntimeError(f'上传失败 ({resp.status_code}): {resp.text[:200]}')


def main():
    if len(sys.argv) < 2:
        print('用法: python upload_to_minio.py <本地文件路径> [远端文件名]')
        sys.exit(1)

    local_path = sys.argv[1]
    remote_path = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        url = upload_file(local_path, remote_path)
        print(json.dumps({'url': url, 'success': True}, ensure_ascii=False))
    except Exception as e:
        print(f'[错误] {e}')
        print(json.dumps({'error': str(e), 'success': False}, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    main()
