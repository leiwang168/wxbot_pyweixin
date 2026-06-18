"""
内容合规检查 — 发送给微信联系人的消息，先过这一关
禁止：泄露别家报价、内部流程表述、AI身份暴露
"""
import re, sys

# 违禁词列表 — 发给微信联系人时绝对不能出现
FORBIDDEN_PATTERNS = [
    # 泄露别家报价
    r'另一家.*(?:报价|价格|报[了]?|单价)',
    r'(?:报价|价格|报[了]?|单价).*另一家',
    r'其他微信联系人.*(?:报价|价格|单价)',
    r'(?:报价|价格|单价).*其他微信联系人',
    r'(?:李铎|李晓博|俱文东|静静).*(?:报价|价格|报[了]?|单价)',
    r'(?:报价|价格|报[了]?|单价).*(?:李铎|李晓博|俱文东|静静)',
    r'(?:他|她|别人|别家).*(?:报价|价格|报[了]?)',
    r'(?:报价|价格|报[了]?).*(?:他|她|别人|别家)',

    # 内部流程暴露
    r'(?:报[给向]|请示|汇报|提交[给到]).*(?:老板|领导|上级|公司)',
    r'(?:老板|领导|上级|公司).*(?:说|看|批|拍板|决定|定)',
    r'等[着]?.*(?:回复|通知|批准|确认|拍板)',
    r'整理[好完].*(?:报|发|提交|给)',
    r'汇总.*(?:报价|价格|对比|表格)',
    r'(?:等|等一[下会]).*(?:领导|老板|公司).*(?:定|批|决定)',

    # AI身份暴露
    r'(?:我|咱)是(?:AI|机器人|人工|程序|软件|模型)',
    r'(?:自动|系统|程序).*(?:发送|回复|生成)',
    r'作为.*(?:AI|助手|智能)',
]

# 但发给老板/自己的渠道，这些词是正常的
# 这条规则只检查发给微信联系人的消息


def check_contact_message(contact: str, message: str) -> list:
    """检查发给微信联系人的消息是否含有违禁内容，返回违规项列表"""
    violations = []
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            violations.append(pattern)
    return violations


def validate(contact: str, message: str) -> bool:
    """前置校验：合规返回True，否则打印错误并返回False"""
    violations = check_contact_message(contact, message)
    if violations:
        print(f'\n❌ [内容合规拦截] 发给 "{contact}" 的消息触发了 {len(violations)} 条规则:')
        for i, v in enumerate(violations, 1):
            print(f'   {i}. 匹配规则: {v}')
        print(f'\n   消息内容: {message[:80]}{"..." if len(message) > 80 else ""}')
        print('   已拦截，未发送。请修改消息后重试。\n')
        return False
    return True


if __name__ == '__main__':
    # 命令行自测
    test_msgs = [
        ('静静', '收到，交期几天？'),
        ('静静', '你好，另一家报了150，你看能不能便宜点？'),
        ('俱文东', '等老板拍板了跟你说'),
        ('李晓博TS', '我整理好对比表报给老板'),
        ('李铎TS', '我是AI助手，自动回复'),
    ]
    print('=== 内容合规自测 ===')
    for s, m in test_msgs:
        ok = validate(s, m)
        print(f'  {"✅" if ok else "❌"} [{s}]: {m[:50]}')
