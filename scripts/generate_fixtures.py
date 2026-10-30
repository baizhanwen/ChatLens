"""Reproducible completely fictional acceptance inputs; never real chat."""
import csv
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
F=ROOT/'fixtures'
F.mkdir(exist_ok=True)
base=datetime(2026,10,26,8,tzinfo=timezone(timedelta(hours=8)))
texts={
 'group':['同学们，项目接口文档已经放在共享目录。','@林舟 请明天下午3点提交接口文档。','建议周五发布，尚未确定。','确定把评审改到10月28日下午2点。','收到','收到','我会今天整理测试结果。','这个方案我不同意，需要讨论。','引用：接口文档已更新','[附件]需求说明.pdf'],
 'customer':['我们需要一个库存看板。','预算是8000元，不含硬件。','报价为7200元，包含三个月维护。','这个价格太贵，我们需要考虑。','有兴趣，想了解部署方式。','请明天提供本地部署方案。','我会10月29日回复报价。','是否可以先看演示？','收到','收到'],
 'personal':['周末见面时间还没有确定。','我会明天整理旅行清单。','请确认车票时间。','不用提交报告了，任务取消。','今天资料已经发送。','可能下周再讨论。','我不同意这个安排。','收到','收到','[语音]暂未转写']}
allrows=[]
for n,(group,variants) in enumerate(texts.items()):
    rows=[]
    for i in range(360):
        content=variants[i%len(variants)]
        row=dict(platform='qq' if n==0 else 'wechat',account_id='fictional-account',conversation_id='demo-'+group,
          conversation_name={'group':'演示 · 项目群','customer':'演示 · 客户青禾','personal':'演示 · 朋友沟通'}[group],
          conversation_type='group' if group=='group' else 'direct',sender_id=['林舟','许禾','周远'][i%3],source_id=group+'-'+str(i),
          sent_at=(base+timedelta(seconds=i*30)).isoformat(),type='attachment' if '附件' in content else 'audio' if '语音' in content else 'quote' if '引用' in content else 'text',
          content=content,revision=0,deleted=False,capture_mode='import',demo=True,timezone='Asia/Shanghai')
        if row['type']=='attachment':row['attachment_name']='需求说明.pdf'
        if row['type']=='quote':row['reference_id']=group+'-'+str(max(0,i-8))
        rows.append(row)
    (F/('demo-'+group+'.jsonl')).write_text('\n'.join(json.dumps(m,ensure_ascii=False) for m in rows)+'\n')
    allrows.extend(rows)
(F/'demo-all.jsonl').write_text('\n'.join(json.dumps(m,ensure_ascii=False) for m in allrows)+'\n')
with (F/'demo-customer.csv').open('w',newline='') as fp:
    fields=list(texts) # fixed schema with all optional row keys
    fields=sorted(set().union(*(r.keys() for r in allrows)))
    writer=csv.DictWriter(fp,fieldnames=fields);writer.writeheader();writer.writerows([r for r in allrows if r['conversation_id']=='demo-customer'])
events=[]
for i in range(8000):
    row=dict(allrows[i%len(allrows)],source_id='stress-'+str(i),conversation_id='stress-'+str(i%3),
             sent_at=(base+timedelta(seconds=(i*37)%8000)).isoformat())
    events.append(row)
for i in range(1000):events.append(dict(events[i]))
for i in range(1000):events.append(dict(events[i],revision=1,deleted=i%2==0,content='[已撤回]' if i%2==0 else '更新后的虚构消息'))
(F/'replay-10000.jsonl').write_text('\n'.join(json.dumps(m,ensure_ascii=False) for m in events)+'\n')
(F/'replay-10000-expected.json').write_text(json.dumps({'events':10000,'unique':8000,'updated':1000,'deleted':500,'visible':7500,'dataset':'fictional'},indent=2))
print(json.dumps({'demo_messages':len(allrows),'replay_events':len(events),'all_fictional':True}))
