"""Management projections; no model calls or outgoing platform writes."""
import time

from murasamebot.bootstrap.runtime import prepare_store


def platform_console(config, platform):
    if platform not in {'qq','bilibili'}:
        raise ValueError('未知平台模块')
    store = prepare_store(config)
    result = store.platform_console(platform)
    owner = store.binding(config.data['owner_person_id'],platform)
    result.update(platform=platform,enabled=platform in config.enabled_platforms,
        owner_id=owner['external_id'] if owner else '',owner_bound=bool(owner))
    if platform=='qq':
        settings = config.data['qq']
        result['connection'] = {'robot_id':settings['robot_id'],'api_url':settings['api_url'],
            'configured':bool(owner and all(settings[k] for k in ('robot_id','access_token','callback_secret'))),
            'last_check':store.get_setting('qq_connection_check',{})}
        with store.connection() as db:
            records=[dict(r) for r in db.execute("""SELECT r.*,json_extract(e.payload,'$.thread_root') AS group_id,
                CASE WHEN r.role='agent' THEN r.account_id ELSE json_extract(e.payload,'$.actor_id') END AS speaker_id,
                CASE WHEN r.role='agent' THEN '丛雨' ELSE json_extract(e.payload,'$.actor_name') END AS speaker_name
                FROM conversation_records r JOIN events e ON e.agent_id=r.agent_id AND e.event_id=r.event_id
                WHERE r.agent_id=? AND r.platform='qq' AND r.visibility='group' AND r.active=1
                ORDER BY r.occurred_at DESC,r.record_id DESC LIMIT 50""",(store.agent_id,))]
        result['groups'] = {'settings':config.data['qq_groups'],'records':records}
        result['batching'] = {k:settings[k] for k in ('quiet_seconds','max_wait_seconds','max_reply_messages')}
        result['autonomy'] = {'settings':config.data['qq_autonomy'],
            'status':store.get_setting('autonomy_status:qq:private',{})}
        result['space'] = {'settings':config.data['qq_space'],
            'status':store.get_setting('autonomy_status:qq:space:dynamic',{})}
    return result


def check_qq(config):
    from murasamebot.adapters.platforms.qq import QQAdapter
    store = prepare_store(config)
    adapter = QQAdapter(config.data['qq'],store,config.data['owner_person_id'])
    try:
        owner = store.binding(config.data['owner_person_id'],'qq')
        adapter.account()
        result = {'account_matches':True,'owner_is_friend':bool(owner and adapter.allows_private_reply(adapter.account_id,owner['external_id'])),
                  'checked_at':time.time(),'sent':False}
        store.set_setting('qq_connection_check',result)
        return result
    finally:
        adapter.close()
