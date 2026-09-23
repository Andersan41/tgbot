import ast
import json
import re
import textwrap
from pathlib import Path

repo=Path(__file__).resolve().parents[2]
path=repo/'fix/bot_fix_v2.0.md'
text=path.read_text(encoding='utf-8')
findings=re.findall(r'^### (B-\d{3}) —',text,re.M)
assert findings==[f'B-{i:03}' for i in range(1,13)],findings
for label in ['I. Executive summary','II. Findings','III. Summary table','IV. Priority order','V. Config recommendations','VI. Questions for the team','VII. Implementation notes for mimo']:
    assert '## '+label in text,label
results=[]
failures=[]
for index, match in enumerate(re.finditer(r'```python\n(.*?)```',text,re.S),1):
    code=textwrap.dedent(match.group(1))
    prefix=text[:match.start()]
    local=prefix[prefix.rfind('\n### B-'):]
    # Only replacement blocks must compile; quoted evidence may be truncated.
    is_fix=(local.rfind('**Fix')>local.rfind('**Evidence') and local.rfind('**Fix')>local.rfind('**Verification'))
    if not is_fix:
        continue
    line=text.count('\n',0,match.start())+1
    try:
        compile(code,f'report:block{index}', 'exec',flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        context='standalone'
    except SyntaxError:
        try:
            compile('async def _insertion_context():\n'+textwrap.indent(code,'    '),f'report:block{index}','exec')
            context='async insertion'
        except SyntaxError:
            try:
                compile('async def _insertion_context():\n    for _item in ():\n'+textwrap.indent(code,'        '),f'report:block{index}','exec')
                context='async loop insertion'
            except SyntaxError as exc:
                failures.append({'block':index,'line':line,'error':str(exc),'code_start':code[:120]})
                continue
    results.append({'block':index,'line':line,'context':context})
output={'findings':findings,'compiled_fix_blocks':len(results),'blocks':results,'failures':failures}
(path.parent/'audit_v2/report-validation.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(output,ensure_ascii=True,indent=2))
assert not failures,failures
