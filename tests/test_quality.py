"""Report quality against fixed labels. Failing target metrics are reported honestly."""
import json
import platform
import unittest
from datetime import datetime, timezone
from pathlib import Path
from services.worker.analysis import analyze
from services.worker.core import normalize

ROOT=Path(__file__).resolve().parents[1]

def evaluate():
    fixture=json.loads((ROOT/'fixtures/quality/annotated-60.json').read_text())
    tp=fp=fn=invalid=owner_correct=due_correct=field_count=0
    failures=[];category_counts={};details=[]
    for sample in fixture['samples']:
        messages=[normalize(m) for m in sample['messages']]
        valid={m['id'] for m in messages};source={m['source_id']:m['id'] for m in messages}
        expected={source[t['source_id']]:t for t in sample['expected_todos']}
        result=analyze(messages,'todos',timezone='Asia/Shanghai',model_config={'mode':'rules'})
        predicted={}
        for item in result['items']:
            refs=item.get('evidence_ids',[])
            invalid+=sum(1 for r in refs if r not in valid)
            for r in refs:predicted[r]=item
        matched=set(expected)&set(predicted);false=set(predicted)-set(expected);missed=set(expected)-set(predicted)
        tp+=len(matched);fp+=len(false);fn+=len(missed)
        field_errors=[]
        for mid in matched:
            field_count+=1
            owner_correct+=predicted[mid].get('owner')==expected[mid]['owner']
            due_correct+=predicted[mid].get('due_at')==expected[mid]['due_at']
            for field in ('owner','due_at'):
                if predicted[mid].get(field)!=expected[mid][field]:field_errors.append({'field':field,'expected':expected[mid][field],'actual':predicted[mid].get(field)})
        category_counts[sample['category']]=category_counts.get(sample['category'],0)+1
        detail={'id':sample['id'],'category':sample['category'],'expected_count':len(expected),'predicted_count':len(predicted),'false_positive':len(false),'false_negative':len(missed),'field_errors':field_errors}
        details.append(detail)
        if false or missed or field_errors:
            failures.append(dict(detail,text=sample['messages'][1]['content']))
    precision=tp/(tp+fp) if tp+fp else 0;recall=tp/(tp+fn) if tp+fn else 0
    report={'dataset':'fixtures/quality/annotated-60.json','dataset_version':fixture['dataset_version'],'generated_at':datetime.now(timezone.utc).isoformat(),'sample_count':len(fixture['samples']),'category_counts':category_counts,'model':'deterministic-rules-v1','real_model_verified':False,'annotation_origin':fixture['annotation_origin'],'evaluation_scope':'synthetic fixed-label development test; not real-model quality or human blind review','counts':{'true_positive':tp,'false_positive':fp,'false_negative':fn,'invalid_evidence':invalid},'precision':round(precision,6),'recall':round(recall,6),'targets':{'precision':.90,'recall':.80,'invalid_evidence':0},'targets_met':{'precision':precision>=.90,'recall':recall>=.80,'invalid_evidence':invalid==0},'field_accuracy_on_matched':{'sample_count':field_count,'owner':round(owner_correct/field_count,6) if field_count else None,'due_at':round(due_correct/field_count,6) if field_count else None},'failures':failures,'samples':details,'system':platform.platform(),'python':platform.python_version()}
    output=ROOT/'docs/evidence';output.mkdir(parents=True,exist_ok=True);(output/'rule-quality.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report

class QualityTests(unittest.TestCase):
    def test_fixed_labels_and_evidence_integrity(self):
        report=evaluate();self.assertEqual(report['sample_count'],60);self.assertEqual(report['category_counts'],{'group':20,'customer':20,'direct':20});self.assertEqual(report['counts']['invalid_evidence'],0)
        # Quality target misses belong in the report, not hidden by adapting expected labels.
        self.assertGreater(report['counts']['true_positive'],0)

if __name__=='__main__':unittest.main()
