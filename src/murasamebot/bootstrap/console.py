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
