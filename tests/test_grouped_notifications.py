import json
import httpx
import pytest
from types import SimpleNamespace
from test_bilibili_contracts import credentials, nav_data, notification
from murasamebot.adapters.platforms.bilibili import BilibiliAdapter


def comment(n, *, actor=123, root=0, parent=0, at=995):
    return {"rpid": n, "oid": 42, "type": 1, "root": root, "parent": parent, "ctime": at,
        "member": {"mid": actor, "uname": "访客"}, "content": {"message": "评论"}}


def make_bot(tmp_path, rows, *, owner=999, cursor=None, failure=False):
    calls=[]
    group=notification(1,"700");group.update(is_multi=1,counts=9);group['item']['root_id']=0
    def handle(request):
        calls.append(request)
        assert request.method=='GET'
        path=request.url.path
        if path.endswith('/nav'):data=nav_data()
        elif path.endswith('/view'):data={"owner":{"mid":owner}}
        elif path.endswith('/wbi/main'):
            if failure:return httpx.Response(200,json={"code":-404})
            data={"replies":rows,"cursor":cursor if cursor is not None else {"is_end":True}}
        elif path.endswith('/reply'):data={"items":[group],"cursor":{"is_end":True}}
        elif path.endswith('/at'):data={"items":[],"cursor":{"is_end":True}}
        else:raise AssertionError(path)
        return httpx.Response(200,json={"code":0,"data":data})
    bot=BilibiliAdapter(credentials(tmp_path),client=httpx.Client(transport=httpx.MockTransport(handle)))
    return bot,calls


def test_group_of_nine_recovers_nine_independent_comments_and_deduplicates_source(tmp_path):
    bot,calls=make_bot(tmp_path,[comment(n) for n in range(700,709)])
    events=bot.notifications(1000)
    assert {e.reply_to for e in events}=={str(n) for n in range(700,709)}
    assert all(e.event_id==f'bilibili:999:comment:1:42:{e.reply_to}' for e in events)
    bot.notifications(1001)
    assert sum(r.url.path.endswith('/wbi/main') for r in calls)==1


def test_only_own_video_group_is_expanded(tmp_path):
    bot,calls=make_bot(tmp_path,[comment(701)],owner=456)
    assert [e.reply_to for e in bot.notifications(1000)]==['700']
    assert not any(r.url.path.endswith('/wbi/main') for r in calls)


def test_nested_comments_only_directly_address_ai_and_age_and_self_are_filtered(tmp_path):
    root=comment(701);root['replies']=[comment(702,actor=999,root=701,parent=701),
        comment(703,actor=234,root=701,parent=702),comment(704,actor=345,root=701,parent=701)]
    bot,_=make_bot(tmp_path,[root,comment(705,at=-100000),comment(706,at=1001),comment(707,actor=999)])
    ids={e.reply_to for e in bot.notifications(1000)}
    assert ids=={'700','701','703'}


def test_missing_cursor_keeps_first_page_and_warns_without_loop(tmp_path):
    bot,calls=make_bot(tmp_path,[comment(701)],cursor={'is_end':False})
    assert {e.reply_to for e in bot.notifications(1000)}=={'700','701'}
    assert bot.take_warnings()
    assert sum(r.url.path.endswith('/wbi/main') for r in calls)==1


def test_read_failure_preserves_normal_notification_and_retries_after_minute(tmp_path):
    bot,calls=make_bot(tmp_path,[],failure=True)
    assert [e.reply_to for e in bot.notifications(1000)]==['700']
    assert bot.take_warnings()
    bot.notifications(1001)
    assert sum(r.url.path.endswith('/wbi/main') for r in calls)==1
    bot.notifications(1061)
    assert sum(r.url.path.endswith('/wbi/main') for r in calls)==2


def test_recovery_pages_are_bounded_to_three_and_batch_limit_still_applies(tmp_path):
    bot,calls=make_bot(tmp_path,[comment(n) for n in range(700,800)],cursor={'is_end':False,'pagination_reply':{'next_offset':'next'}})
    events=bot.notifications(1000)
    assert len(events)==90 and bot.take_warnings()
    assert sum(r.url.path.endswith('/wbi/main') for r in calls)<=3


def test_restart_preserves_processed_representative_and_recovers_eight(factory,tmp_path):
    from murasamebot.bootstrap.platforms.bilibili import BilibiliModule
    e,s,_,_=factory()
    bot,_=make_bot(tmp_path,[comment(n) for n in range(700,709)])
    first=bot.notification_event(notification(1,'700'),'reply','999',1000)
    e.ingest(first);s.finish_event(first.event_id,'done')
    s.set_setting('notifications_initialized:999',True)
    module=BilibiliModule('bilibili',bot)
    module._poll_notifications(SimpleNamespace(store=s,engine=e),1000)
    states=[r['state'] for r in s.inspect('events')]
    assert states.count('done')==1 and states.count('pending')==8
    rebuilt,calls=make_bot(tmp_path,[comment(n) for n in range(700,709)])
    BilibiliModule('bilibili',rebuilt)._poll_notifications(SimpleNamespace(store=s,engine=e),1001)
    assert len(s.inspect('events'))==9
    assert not any(r.url.path.endswith('/wbi/main') for r in calls)


def test_first_start_establishes_baseline_including_recovered_comments(factory,tmp_path):
    from murasamebot.bootstrap.platforms.bilibili import BilibiliModule
    e,s,_,_=factory();bot,_=make_bot(tmp_path,[comment(n) for n in range(700,709)])
    BilibiliModule('bilibili',bot)._poll_notifications(SimpleNamespace(store=s,engine=e),1000)
    assert [r['state'] for r in s.inspect('events')]==['baseline']*9
