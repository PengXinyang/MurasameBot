"""Exercise the actual shared renderer without a browser or account."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest


def test_state_zero_labels_and_state17_retry_controls():
    node=shutil.which('node')
    if not node:pytest.skip('Node required for actual frontend renderer')
    path=Path(__file__).resolve().parents[1]/'src/murasamebot/bootstrap/console-common.js'
    js=r'''
const fs=require('fs'),vm=require('vm');
const ctx={esc:x=>String(x??''),time:x=>String(x),names:{accepted:'平台已受理'}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
function row(state,status='account_visible',extra={}) {return {action_id:'one',state:'accepted',created_at:1,
 payload:JSON.stringify({kind:'reply',platform:'bilibili',visibility:'public',content:{provider:'bilibili',kind:'video'},text:'正文'}),
 receipt:JSON.stringify({state:'accepted',external_id:'100',detail:'平台已受理，公开可见性尚未独立验证',
 publication_check:{platform_state:state,status,detail:'尚不能证明其他账号可见',checked_at:1}}),...extra}}
const normal=ctx.renderActionRow(row(0));
const retry=ctx.renderActionRow(row(17,'self_only'));
const unknown=ctx.renderActionRow(row(17,'self_only',{state:'unknown'}));
const folded=ctx.renderActionRow(row(0,'folded'));
console.log(JSON.stringify({normal, retry, unknown, folded}));
'''
    r=subprocess.run([node,'-e',js,str(path)],capture_output=True,text=True,encoding='utf-8',check=True)
    data=json.loads(r.stdout)
    assert '评论已发布' in data['normal'] and '评论状态正常' in data['normal']
    assert '尚未独立验证' not in data['normal'] and '尚不能证明' not in data['normal']
    assert 'data-op="actions/send"' not in data['normal']
    assert 'data-op="actions/send"' in data['retry'] and '>重发</button>' in data['retry']
    assert 'data-op="actions/send"' not in data['unknown']
    assert '评论状态正常' not in data['folded']
