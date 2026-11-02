"""Run the actual frontend state selector and renderer in Node."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_status_selector_counts_selection_and_private_review_label():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for frontend checks")
    path = Path(__file__).resolve().parents[1]/"src/murasamebot/bootstrap/console-common.js"
    script = r'''
const fs=require('fs'),vm=require('vm');
const selection={},root={innerHTML:'',querySelector:()=>selection};
const storage={qq:'failed',bili:'sent',bad:'__proto__'};
const ctx={esc:x=>String(x??''),time:String,names:{queued:'待发送'},
 document:{getElementById:()=>root},sessionStorage:{getItem:k=>storage[k]}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
let selected;
ctx.renderActionStatusFilter('filter','failed',{accepted:61,failed:1,draft:2,reviewing:1,queued:4,unknown:1},v=>selected=v);
selection.onchange({target:{value:'sent'}});
const html=ctx.renderActionRow({state:'queued',action_id:'one',created_at:1,payload:JSON.stringify({
 kind:'private_reply',platform:'qq',visibility:'private',text:'合成回复',review_state:'not_required',review_reason:'当前私聊渠道无需发布审核'})});
console.log(JSON.stringify({options:root.innerHTML,selected,html,
 qq:ctx.storedActionStatus('qq'),bili:ctx.storedActionStatus('bili'),bad:ctx.storedActionStatus('bad')}));
'''
    result = subprocess.run([node,"-e",script,str(path)],capture_output=True,text=True,encoding="utf-8",check=True)
    data = json.loads(result.stdout)
    assert 'value="failed" selected' in data["options"]
    for text in ("全部消息（70）", "已发送（61）", "待审核（3）", "发送失败（1）", "结果待核对（1）"):
        assert text in data["options"]
    assert data["selected"] == "sent"
    assert (data["qq"],data["bili"],data["bad"]) == ("failed","sent","all")
    assert "无需发布审核" in data["html"] and "模型审核通过" not in data["html"]
