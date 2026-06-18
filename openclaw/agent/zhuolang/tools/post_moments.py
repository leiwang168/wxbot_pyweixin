"""
AI自动发朋友圈工具 — 完整版

核心流程：
  1. 从 OSS 素材库随机选图/视频
  2. 识别媒体内容（用图片理解能力）
  3. AI 结合素材内容和业务背景生成朋友圈文案
  4. 发给你确认 → 你确认后发出
  
素材库格式：
  config/materials.json — 素材清单（URL + 标签 + 场景分类）

用法:
  # 一键生成并等待确认
  python post_moments.py --instance wx_001
  
  # 指定场景
  python post_moments.py --scene daily --instance wx_001
  
  # 指定具体素材
  python post_moments.py --material-id "ipa_001" --instance wx_001
  
  # 纯文字模板（不用素材）
  python post_moments.py --template --instance wx_001

素材管理:
  # 列出所有素材
  python post_moments.py --list-materials --instance wx_001
  
  # 添加素材
  python post_moments.py --add-material --url "https://..." --tags "IPA,新品" --instance wx_001
"""
import sys, os, json, random, argparse, uuid, shlex
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from mqtt_client import ProcurementAgent
from _content_check import validate

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent
MATERIALS_FILE = REPO_ROOT / 'config' / 'moments_materials.json'

# ====== 默认素材库（项目内建示例） ======
# 实际素材老板维护在 config/moments_materials.json 或自己定义在OSS上
DEFAULT_MATERIALS = [
    {
        "id": "daily_001",
        "url": "https://minio-uat.frp.datamavin.cn/wbot-zhuolang/static/daily_work.jpg",
        "type": "image",
        "scene": "daily",
        "tags": ["日常", "工作"],
        "description": "工作日常照片",
    },
    {
        "id": "product_ipa_001",
        "url": "https://minio-uat.frp.datamavin.cn/wbot-zhuolang/static/ipa_pour.jpg",
        "type": "image",
        "scene": "product",
        "tags": ["IPA", "精酿", "倒酒"],
        "description": "IPA倒酒特写",
    },
    {
        "id": "brewery_001",
        "url": "https://minio-uat.frp.datamavin.cn/wbot-zhuolang/static/brewery_tank.jpg",
        "type": "image",
        "scene": "brewery",
        "tags": ["酒厂", "发酵罐"],
        "description": "酒厂发酵车间",
    },
    {
        "id": "product_wheat_001",
        "url": "https://minio-uat.frp.datamavin.cn/wbot-zhuolang/static/wheat_beer.jpg",
        "type": "image",
        "scene": "product",
        "tags": ["小麦啤", "精酿"],
        "description": "小麦啤品鉴",
    },
    {
        "id": "seasonal_summer_001",
        "url": "https://minio-uat.frp.datamavin.cn/wbot-zhuolang/static/summer_fruit_beer.jpg",
        "type": "image",
        "scene": "seasonal",
        "tags": ["夏季", "水果", "果味"],
        "description": "夏季果味精酿",
    },
    {
        "id": "team_001",
        "url": "https://minio-uat.frp.datamavin.cn/wbot-zhuolang/static/team_photo.jpg",
        "type": "image",
        "scene": "team",
        "tags": ["团队", "工作"],
        "description": "团队工作照",
    },
]


# ====== 背景知识（给AI生成文案用） ======
BRAND_CONTEXT = """
浊浪精酿 — 精酿啤酒品牌
产品线：4大系列24款
- 基础系列（经典拉格、小麦啤）：大众入门，走量
- 果味系列（果泥酸、水果小麦）：年轻人偏好，酒吧引流
- 匠心系列（IPA、世涛）：精酿发烧友
- 气泡米酒系列：低度微醺，餐吧场景

酒厂实力：50万吨产能，三大生产基地，服务海底捞、盒马
产品包装：20L PET桶装鲜啤
储存方式：5-25℃避光，保质期90天
全国物流：2-4天到货

微信朋友圈人设：真实精酿销售，不装逼，说实话，像真人发朋友圈
禁止：AI腔、官方腔、堆砌术语、暴露AI身份
"""

# ====== 纯文字模板（不需要图片的备选） ======
TEXT_TEMPLATES = {
    'daily': [
        "又是干活的一天 💪",
        "晚安，明天继续",
        "周末了，喝一杯🍺",
        "每个工作日都值得认真对待",
        "生活不易，多喝好酒",
        "新的一周，冲冲冲",
        "记录一下今天的工作",
    ],
    'new_product': [
        "新批次IPA刚到，香气炸裂🍺",
        "今天到了新货，品控稳定",
        "刚开封测了一轮，这批品质高",
    ],
    'beer_knowledge': [
        "精酿不只有IPA，小麦啤也很好喝",
        "好酒不需要太多解释，入口就知道",
        "20L桶装鲜啤，保质期90天，囤货无忧",
    ],
    'brewery': [
        "50万吨产能，三大生产基地，品质有保障",
        "好原料酿好酒，全球精选",
        "从发酵罐到餐桌，每一步都认真",
    ],
    'seasonal': [
        "夏天来了，果味系列走起来🍑",
        "天热了，酸啤和果味小麦是这个季节的王炸",
        "天冷了，整点世涛暖暖身☕",
    ],
}

# ====== 素材库管理 ======

def load_materials():
    """加载素材库"""
    if MATERIALS_FILE.exists():
        try:
            with open(MATERIALS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    # 首次使用，创建默认素材库
    save_materials(DEFAULT_MATERIALS)
    return DEFAULT_MATERIALS


def save_materials(materials):
    """保存素材库到文件"""
    MATERIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MATERIALS_FILE, 'w', encoding='utf-8') as f:
        json.dump(materials, f, ensure_ascii=False, indent=2)
    return True


def add_material(url, type='image', scene='daily', tags=None, description=''):
    """添加素材到素材库"""
    materials = load_materials()
    new_id = f'{scene}_{uuid.uuid4().hex[:6]}'
    material = {
        "id": new_id,
        "url": url,
        "type": type,
        "scene": scene,
        "tags": tags or [],
        "description": description,
    }
    materials.append(material)
    save_materials(materials)
    return new_id


def get_random_material(scene=None):
    """获取随机素材"""
    materials = load_materials()
    if not materials:
        return None
    if scene:
        filtered = [m for m in materials if m.get('scene') == scene]
        if not filtered:
            return None
        return random.choice(filtered)
    # 按场景分别取一个（避免某类素材垄断），再从候选中随机
    scenes = {}
    for m in materials:
        s = m.get('scene', 'daily')
        if s not in scenes:
            scenes[s] = []
        scenes[s].append(m)
    # 每个场景选一个，再从这些里面随机
    candidates = []
    for s_list in scenes.values():
        candidates.append(random.choice(s_list))
    return random.choice(candidates)


def get_material_by_id(material_id):
    """按ID获取素材"""
    materials = load_materials()
    for m in materials:
        if m.get('id') == material_id:
            return m
    return None


def list_materials(scene=None):
    """列出素材"""
    materials = load_materials()
    if scene:
        return [m for m in materials if m.get('scene') == scene]
    return materials


# ====== 朋友圈发布（走MQTT） ======

def publish_moments(text, media_files=None, instance='wx_001', timeout=60):
    """发朋友圈 — 走MQTT"""
    if isinstance(media_files, str):
        media_files = media_files.strip().split()
    if media_files and len(media_files) > 9:
        return {'ok': False, 'error': '最多9张图/视频', 'instance': instance}
    if not text and not media_files:
        return {'ok': False, 'error': '文字和媒体文件至少有一个', 'instance': instance}

    agent = ProcurementAgent(instance_id=instance)
    if not agent.connect():
        return {'ok': False, 'error': 'MQTT连接失败', 'instance': instance}

    cid, callback = agent.post_moments(
        text=text,
        media_files=media_files or None,
        timeout=timeout,
    )

    error = ''
    if callback:
        result = callback.get('result', {})
        if isinstance(result, dict):
            error = result.get('error', '')
    ok = bool(cid and not error)
    agent.disconnect()
    return {'ok': ok, 'cid': cid, 'error': error, 'text': text, 'instance': instance}


# ====== 文案管理（暂存待确认） ======

PENDING_FILE = REPO_ROOT / 'records' / 'moments_pending.json'


def save_pending(item):
    """保存待确认的文案"""
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    pendings = []
    if PENDING_FILE.exists():
        try:
            pendings = json.loads(PENDING_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    pendings.append(item)
    PENDING_FILE.write_text(json.dumps(pendings, ensure_ascii=False, indent=2), encoding='utf-8')


def get_pending():
    """获取所有待确认"""
    if not PENDING_FILE.exists():
        return []
    try:
        return json.loads(PENDING_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []


def clear_pending(item_id=None):
    """清除待确认（发布后或拒绝后）"""
    if not PENDING_FILE.exists():
        return True
    if item_id:
        pendings = get_pending()
        pendings = [p for p in pendings if p.get('id') != item_id]
        PENDING_FILE.write_text(json.dumps(pendings, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        PENDING_FILE.unlink(missing_ok=True)
    return True


# ====== 主函数 ======

def main():
    parser = argparse.ArgumentParser(
        description='AI 自动发朋友圈工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--instance', required=True, help='Wbot实例ID')

    # 素材选择
    parser.add_argument('--scene', '-s', default='', help='素材场景（daily/product/brewery/seasonal/team）')
    parser.add_argument('--material-id', '-mid', default='', help='指定素材ID')
    parser.add_argument('--template-only', '-t', action='store_true', help='仅用文字模板，不用素材')

    # 文案自定义
    parser.add_argument('--text', default='', help='直接指定文案（跳过AI生成）')
    parser.add_argument('--media', '-m', default='', help='直接指定媒体URL')

    # 素材管理
    parser.add_argument('--list-materials', action='store_true', help='列出素材库')
    parser.add_argument('--add-material', action='store_true', help='添加素材到库')
    parser.add_argument('--url', default='', help='素材URL')
    parser.add_argument('--tags', default='', help='标签（逗号分隔）')
    parser.add_argument('--desc', default='', help='素材描述')

    # 待确认管理
    parser.add_argument('--list-pending', action='store_true', help='列出待确认的文案')
    parser.add_argument('--confirm', default='', help='确认发布指定ID的待确认文案')
    parser.add_argument('--reject', default='', help='拒绝指定ID的待确认文案')

    # 输出
    parser.add_argument('--json', action='store_true', help='输出JSON格式')
    parser.add_argument('--timeout', type=int, default=60, help='发朋友圈超时(秒)')

    args = parser.parse_args()

    # ========== 素材管理 ==========
    if args.list_materials:
        materials = list_materials(args.scene or None)
        if args.json:
            print(json.dumps(materials, ensure_ascii=False, indent=2))
        else:
            if not materials:
                print("📦 素材库为空")
                return
            print(f"\n📦 素材库 ({len(materials)}个)")
            print("=" * 60)
            for m in materials:
                tags_str = ','.join(m.get('tags', [])) if m.get('tags') else ''
                print(f"  [{m['id']}] {m.get('scene','?')} | {m.get('type','?')}")
                print(f"     URL: {m['url']}")
                if tags_str:
                    print(f"     标签: {tags_str}")
                if m.get('description'):
                    print(f"     描述: {m['description']}")
                print()
        return

    if args.add_material:
        if not args.url:
            print("❌ 需要 --url 参数")
            sys.exit(1)
        new_id = add_material(args.url, scene=args.scene or 'daily', tags=[t.strip() for t in args.tags.split(',') if t.strip()], description=args.desc)
        result = {'ok': True, 'material_id': new_id, 'url': args.url}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"✅ 素材已添加: {new_id}")
        return

    # ========== 待确认管理 ==========
    if args.list_pending:
        pendings = get_pending()
        if args.json:
            print(json.dumps(pendings, ensure_ascii=False, indent=2))
        else:
            if not pendings:
                print("📋 暂无待确认的文案")
                return
            print(f"\n📋 待确认文案 ({len(pendings)}条)")
            print("=" * 60)
            for p in pendings:
                print(f"  [{p['id']}] {p.get('scene','?')} | 生成于 {p.get('created_at','?')}")
                print(f"    文案: {p.get('text','')}")
                if p.get('media_urls'):
                    print(f"    媒体: {p['media_urls']}")
                print()
        return

    if args.confirm:
        pendings = get_pending()
        target = None
        for p in pendings:
            if p['id'] == args.confirm:
                target = p
                break
        if not target:
            print(f"❌ 未找到待确认文案: {args.confirm}")
            sys.exit(1)
        # 发朋友圈
        result = publish_moments(
            text=target.get('text', ''),
            media_files=target.get('media_urls', []),
            instance=args.instance,
            timeout=args.timeout,
        )
        if result['ok']:
            clear_pending(args.confirm)
            log_path = REPO_ROOT / 'records' / 'moments_history.json'
            logs = []
            if log_path.exists():
                try:
                    logs = json.loads(log_path.read_text(encoding='utf-8'))
                except Exception:
                    pass
            logs.append({**target, 'published_at': datetime.now().isoformat(), 'ok': True, 'cid': result.get('cid')})
            log_path.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding='utf-8')
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"✅ 朋友圈已发布！")
                print(f"   文案: {target.get('text','')[:50]}")
                if target.get('media_urls'):
                    print(f"   媒体: {target['media_urls']}")
        else:
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"❌ 发布失败: {result.get('error','')}")
        return

    if args.reject:
        clear_pending(args.reject)
        result = {'ok': True, 'rejected_id': args.reject}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"🗑️ 已拒绝文案: {args.reject}")
        return

    # ========== 生成 + 发送 ==========
    # 如果用户直接给了 text 和 media，直接发（跳过AI生成）
    if args.text and args.media:
        if not validate('朋友圈', args.text):
            sys.exit(1)
        result = publish_moments(args.text, args.media, instance=args.instance, timeout=args.timeout)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif result['ok']:
            print(f"✅ 朋友圈已发送")
            print(f"   文案: {args.text[:50]}...")
        else:
            print(f"❌ 发送失败: {result.get('error','')}")
            sys.exit(1)
        return

    if args.text:
        # 只有文字，没有media
        if not validate('朋友圈', args.text):
            sys.exit(1)
        result = publish_moments(args.text, None, instance=args.instance, timeout=args.timeout)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif result['ok']:
            print(f"✅ 朋友圈已发送")
            print(f"   文案: {args.text[:50]}...")
        else:
            print(f"❌ 发送失败: {result.get('error','')}")
            sys.exit(1)
        return

    # === AI生成流程 ===
    # 1. 选素材
    media_urls = []
    material_desc = ''

    if args.template_only:
        # 纯文字模板
        scene = args.scene or 'daily'
        pool = TEXT_TEMPLATES.get(scene, TEXT_TEMPLATES.get('daily', ['🌞']))
        gen_text = random.choice(pool)
        media_urls = []
    elif args.material_id:
        mat = get_material_by_id(args.material_id)
        if not mat:
            print(f"❌ 未找到素材: {args.material_id}")
            sys.exit(1)
        media_urls = [mat['url']]
        material_desc = mat.get('description', '') or ','.join(mat.get('tags', []))
        scene = mat.get('scene', 'daily')
    elif args.scene:
        mat = get_random_material(args.scene)
        if not mat:
            # 该场景无素材，降级到纯文字模板
            scene = args.scene
            pool = TEXT_TEMPLATES.get(scene, TEXT_TEMPLATES.get('daily', ['🌞']))
            gen_text = random.choice(pool)
            media_urls = []
        else:
            media_urls = [mat['url']]
            material_desc = mat.get('description', '') or ','.join(mat.get('tags', []))
            scene = mat.get('scene', args.scene)
    else:
        mat = get_random_material()
        if mat:
            media_urls = [mat['url']]
            material_desc = mat.get('description', '') or ','.join(mat.get('tags', []))
            scene = mat.get('scene', 'daily')

    # 2. AI生成文案（走大模型能力：由上层agent调用时生成，这里预留接口）
    # 这个函数本身不调用大模型，而是在agent侧通过prompt生成文案
    # 返回一个结构化的"待确认项"
    if args.template_only or (media_urls == [] and not args.media):
        # 纯文字 → 直接发（不需要确认）
        if not validate('朋友圈', gen_text):
            sys.exit(1)
        result = publish_moments(gen_text, None, instance=args.instance, timeout=args.timeout)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif result['ok']:
            print(f"✅ 朋友圈已发送（文字模板）")
            print(f"   文案: {gen_text[:50]}...")
        else:
            print(f"❌ 发送失败: {result.get('error','')}")
            sys.exit(1)
        return

    # 有素材但未指定文案 → 输出结构体让agent生成文案后调用确认流程
    pending_id = f"moments_{uuid.uuid4().hex[:8]}"
    pending_item = {
        "id": pending_id,
        "scene": scene,
        "media_urls": media_urls,
        "material_desc": material_desc,
        "text": "",  # AI生成后填充
        "status": "pending_review",
        "created_at": datetime.now().isoformat(),
    }
    save_pending(pending_item)

    result = {
        "ok": True,
        "pending_id": pending_id,
        "pending_material": True,
        "media_urls": media_urls,
        "scene": scene,
        "message": f"素材已选定 [{pending_id}]，请用AI生成文案后调用 --confirm {pending_id} 发布",
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"\n📋 已选定素材 -> 待确认ID: {pending_id}")
        print(f"   媒体: {media_urls}")
        print(f"   场景: {scene}")
        print(f"   素材描述: {material_desc}")
        print()
        print("💡 现在由你（AI）生成朋友圈文案，确认后发给老板看")
        print("   老板确认无误后，执行:")
        print(f"   python post_moments.py --confirm {pending_id} --instance {args.instance}")
        print()


if __name__ == '__main__':
    main()
