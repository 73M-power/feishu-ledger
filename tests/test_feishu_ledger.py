from datetime import datetime, timezone, timedelta

import feishu_ledger as L


CST = timezone(timedelta(hours=8))


def test_parse_aa_expense_with_members():
    entry = L.parse_expense(
        '今天买菜 83 AA',
        sender={'name': 'Maya'},
        settings={'members': ['Maya', 'Alex'], 'currency': 'CNY'},
        now=datetime(2026, 7, 7, 20, 0, tzinfo=CST),
    )
    assert entry['date'] == '2026-07-07'
    assert entry['amount'] == 83
    assert entry['category'] == '买菜'
    assert entry['payer'] == 'Maya'
    assert entry['participants'] == ['Maya', 'Alex']
    assert entry['shares'] == {'Maya': 41.5, 'Alex': 41.5}


def test_month_summary_and_settlement(tmp_path):
    data_dir = tmp_path
    L.update_settings(data_dir, {'members': ['Maya', 'Alex']})
    L.add_expense(data_dir, '今天买菜 80 AA', sender={'name': 'Maya'}, now=datetime(2026, 7, 7, 12, 0))
    L.add_expense(data_dir, '今天外卖 40 AA', sender={'name': 'Alex'}, now=datetime(2026, 7, 7, 18, 0))
    ledger = L.load_ledger(data_dir)
    summary = L.month_summary(ledger, '2026-07')
    assert summary['total'] == 120
    assert summary['by_category']['买菜'] == 80
    assert summary['by_category']['餐饮'] == 40
    assert summary['balances'] == {'Alex': -20.0, 'Maya': 20.0}
    assert summary['settlements'] == [{'from': 'Alex', 'to': 'Maya', 'amount': 20.0}]


def test_chat_command_comparison(tmp_path):
    L.update_settings(tmp_path, {'members': ['Maya', 'Alex']})
    L.add_expense(tmp_path, '6月30日超市 60 AA', sender={'name': 'Maya'}, now=datetime(2026, 7, 7, 12, 0))
    L.add_expense(tmp_path, '7月1日外卖 90 AA', sender={'name': 'Maya'}, now=datetime(2026, 7, 7, 12, 0))
    result = L.handle_chat_command(tmp_path, '账本对比', now=datetime(2026, 7, 7, 12, 0))
    assert result['ok'] is True
    assert '2026-06' in result['reply']
    assert '2026-07' in result['reply']


def test_feishu_v2_message_parsing():
    payload = {
        'schema': '2.0',
        'header': {'event_id': 'evt_1', 'create_time': '1783699200000'},
        'event': {
            'sender': {'sender_id': {'open_id': 'ou_1'}, 'sender_type': 'user'},
            'message': {
                'message_id': 'om_1',
                'chat_id': 'oc_1',
                'message_type': 'text',
                'content': '{"text":"今天买菜 83 AA"}',
            },
        },
    }
    msg = L.parse_feishu_message(payload)
    assert msg['kind'] == 'message'
    assert msg['event_id'] == 'evt_1'
    assert msg['message_id'] == 'om_1'
    assert msg['create_time'] == '1783699200000'
    assert msg['text'] == '今天买菜 83 AA'



def test_income_and_delete_commands(tmp_path):
    income = L.handle_chat_command(
        tmp_path,
        '收入3000 工资',
        sender={'name': 'Maya'},
        now=datetime(2026, 7, 7, 12, 0, tzinfo=CST),
    )
    assert income['ok'] is True
    assert income['entry']['type'] == 'income'
    assert income['summary']['income_total'] == 3000

    expense = L.handle_chat_command(
        tmp_path,
        '今天买菜100',
        sender={'name': 'Maya'},
        now=datetime(2026, 7, 7, 13, 0, tzinfo=CST),
    )
    assert expense['ok'] is True
    summary = L.month_summary(L.load_ledger(tmp_path), '2026-07')
    assert summary['income_total'] == 3000
    assert summary['expense_total'] == 100
    assert summary['net_total'] == 2900

    deleted = L.handle_chat_command(
        tmp_path,
        '删除上一笔',
        sender={'name': 'Maya'},
        now=datetime(2026, 7, 7, 14, 0, tzinfo=CST),
    )
    assert deleted['ok'] is True
    assert deleted['deleted']['description'] == '买菜'
    summary = L.month_summary(L.load_ledger(tmp_path), '2026-07')
    assert summary['income_total'] == 3000
    assert summary['expense_total'] == 0


def test_detail_and_export_commands(tmp_path):
    L.handle_chat_command(tmp_path, '收入3000 工资', sender={'name': 'Maya'}, now=datetime(2026, 7, 7, 12, 0, tzinfo=CST))
    detail = L.handle_chat_command(tmp_path, '账本明细', now=datetime(2026, 7, 7, 12, 0, tzinfo=CST))
    assert '收入' in detail['reply']
    export = L.handle_chat_command(tmp_path, '导出本月', now=datetime(2026, 7, 7, 12, 0, tzinfo=CST))
    assert '/api/ledger/export.csv?month=2026-07' in export['reply']



def test_bitable_entry_fields():
    import feishu_bitable as B

    fields = B.entry_fields({
        'id': 'REC001',
        'type': 'income',
        'date': '2026-07-10',
        'time': '15:10',
        'category': '工资',
        'description': '7月工资',
        'amount': 3000,
        'receiver': 'Maya',
        'raw_text': '收入3000 工资',
    })
    assert fields['记录ID'] == 'REC001'
    assert fields['类型'] == '收入'
    assert fields['金额'] == 3000
    assert isinstance(fields['日期'], int)


def test_income_amount_ignores_month_number_in_note():
    entry = L.parse_income(
        '入账：6000，备注：7月公粮',
        sender={'name': 'Maya'},
        now=datetime(2026, 7, 9, 14, 0, tzinfo=CST),
    )
    assert entry['amount'] == 6000
    assert entry['description'] == '7月公粮'


def test_duplicate_event_is_silent(tmp_path):
    source = {'platform': 'feishu', 'event_id': 'evt_dup'}
    first = L.handle_chat_command(
        tmp_path,
        '收入6000 工资',
        sender={'name': 'Maya'},
        source=source,
        now=datetime(2026, 7, 9, 14, 0, tzinfo=CST),
    )
    second = L.handle_chat_command(
        tmp_path,
        '收入6000 工资',
        sender={'name': 'Maya'},
        source=source,
        now=datetime(2026, 7, 9, 14, 0, tzinfo=CST),
    )
    assert first['ok'] is True
    assert second['duplicate'] is True
    assert second['reply'] == ''


def test_summary_command_does_not_build_bitable_summary_rows(tmp_path):
    L.handle_chat_command(tmp_path, '收入6000 工资', sender={'name': 'Maya'}, now=datetime(2026, 7, 9, 10, 0, tzinfo=CST))
    L.handle_chat_command(tmp_path, '7月10日买菜100', sender={'name': 'Maya'}, now=datetime(2026, 7, 9, 11, 0, tzinfo=CST))
    result = L.handle_chat_command(tmp_path, '总结', sender={'name': 'Maya'}, now=datetime(2026, 7, 9, 12, 0, tzinfo=CST))
    assert result['ok'] is True
    assert result['summary']['income_total'] == 6000
    assert result['summary']['expense_total'] == 100
    assert 'entries_to_sync' not in result


def test_bitable_summary_fields_use_existing_type_options():
    import feishu_bitable as B

    fields = B.entry_fields({
        'id': 'sum_2026-07_net',
        'type': 'summary',
        'display_type': '收入',
        'date': '2026-07-09',
        'time': '12:00',
        'category': '结余',
        'description': '2026-07 结余',
        'amount': 5900,
        'receiver': '账本总结',
        'raw_text': '总结',
    })
    assert fields['类型'] == '收入'
    assert fields['类别'] == '结余'
    assert fields['描述'] == '2026-07 结余'
    assert fields['金额'] == 5900


def test_bitable_record_item_to_entry_skips_summary_rows():
    import feishu_bitable as B

    assert B.record_item_to_entry({'record_id': 'rec1', 'fields': {'记录ID': 'sum_2026-07_net'}}) is None
    entry = B.record_item_to_entry({
        'record_id': 'rec2',
        'fields': {
            '记录ID': 'inc_1',
            '类型': '收入',
            '日期': 1783526400000,
            '时间': '10:00',
            '类别': '工资',
            '描述': '7月工资',
            '金额': 6000,
            '付款人/收款人': 'Maya',
            '原始消息': '收入6000 工资',
        },
    })
    assert entry['type'] == 'income'
    assert entry['amount'] == 6000
    assert entry['receiver'] == 'Maya'


def test_bitable_record_item_to_entry_skips_unknown_type():
    import feishu_bitable as B

    assert B.record_item_to_entry({
        'record_id': 'rec_unknown',
        'fields': {
            '记录ID': 'rec_unknown',
            '类型': '结余',
            '日期': '2026-07-09',
            '金额': 999,
        },
    }) is None


def test_month_summary_can_use_bitable_entries():
    import feishu_bitable as B

    items = [
        {'record_id': 'rec1', 'fields': {'记录ID': 'inc_1', '类型': '收入', '日期': '2026-07-09', '类别': '工资', '描述': '工资', '金额': 6000, '付款人/收款人': 'Maya'}},
        {'record_id': 'rec2', 'fields': {'记录ID': 'exp_1', '类型': '支出', '日期': '2026-07-09', '类别': '买菜', '描述': '买菜', '金额': 100, '付款人/收款人': 'Maya', '参与人': 'Maya', '分摊': 'Maya:100'}},
        {'record_id': 'rec3', 'fields': {'记录ID': 'sum_2026-07_expense', '类型': '支出', '日期': '2026-07-09', '类别': '月度总结', '描述': '2026-07 总支出', '金额': 100}},
    ]
    entries = [e for e in (B.record_item_to_entry(item) for item in items) if e]
    summary = L.month_summary({'entries': entries}, '2026-07')
    assert summary['income_total'] == 6000
    assert summary['expense_total'] == 100
    assert summary['net_total'] == 5900


def test_use_help_alias(tmp_path):
    result = L.handle_chat_command(tmp_path, '使用帮助')
    assert result['ok'] is True
    assert '账本' in result['reply']

def test_callback_dedupe_is_separate_from_entry_dedupe(tmp_path):
    import server

    server._mark_event_seen(tmp_path, 'evt_callback', 'processing')
    assert server._event_seen(tmp_path, 'evt_callback') is True
    data = L.load_ledger(tmp_path)
    assert data['seen_callbacks']['evt_callback'] == 'processing'
    assert 'evt_callback' not in data.get('seen_events', {})

def test_stale_feishu_callback_is_detected():
    import server

    now = datetime(2026, 7, 10, 0, 22, tzinfo=CST)
    old_ms = int(datetime(2026, 7, 10, 0, 0, tzinfo=CST).timestamp() * 1000)
    fresh_ms = int(datetime(2026, 7, 10, 0, 20, tzinfo=CST).timestamp() * 1000)
    assert server._message_is_stale({'create_time': str(old_ms)}, now) is True
    assert server._message_is_stale({'create_time': str(fresh_ms)}, now) is False
    assert server._message_is_stale({}, now) is False

def test_server_summary_does_not_sync_rows_by_default(tmp_path):
    import server

    L.handle_chat_command(
        tmp_path,
        '收入100 工资',
        sender={'name': 'Maya'},
        now=datetime(2026, 7, 10, 9, 0, tzinfo=CST),
    )
    result = server._summary_command_result(tmp_path, '总结', datetime(2026, 7, 10, 9, 30, tzinfo=CST))
    assert result['ok'] is True
    assert result['summary']['income_total'] == 100
    assert 'entries_to_sync' not in result

def test_history_summary_command_groups_by_month(tmp_path):
    L.handle_chat_command(tmp_path, '收入100 工资', sender={'name': 'Maya'}, now=datetime(2026, 7, 10, 9, 0, tzinfo=CST))
    L.handle_chat_command(tmp_path, '6月10日买菜50', sender={'name': 'Maya'}, now=datetime(2026, 7, 10, 10, 0, tzinfo=CST))
    result = L.handle_chat_command(tmp_path, '历史总结', sender={'name': 'Maya'}, now=datetime(2026, 7, 10, 11, 0, tzinfo=CST))
    assert result['ok'] is True
    assert result['history'] is True
    assert '2026-07' in result['reply']
    assert '2026-06' in result['reply']


def test_server_current_and_history_summary_are_separate(tmp_path):
    import server

    L.handle_chat_command(tmp_path, '收入100 工资', sender={'name': 'Maya'}, now=datetime(2026, 7, 10, 9, 0, tzinfo=CST))
    L.handle_chat_command(tmp_path, '6月10日买菜50', sender={'name': 'Maya'}, now=datetime(2026, 7, 10, 10, 0, tzinfo=CST))
    current = server._summary_command_result(tmp_path, '当月总结', datetime(2026, 7, 10, 11, 0, tzinfo=CST))
    history = server._summary_command_result(tmp_path, '历史总结', datetime(2026, 7, 10, 11, 0, tzinfo=CST))
    assert current['summary']['month'] == '2026-07'
    assert current['summary']['income_total'] == 100
    assert current['summary']['expense_total'] == 0
    assert history['history'] is True
    assert '2026-06' in history['reply']
    assert 'entries_to_sync' not in history


def test_cleanup_summary_rows_command_deletes_sum_records(monkeypatch):
    import server

    called = {}

    def fake_delete(prefix, token_provider):
        called['prefix'] = prefix
        return {'ok': True, 'deleted': 2, 'failed': 0}

    monkeypatch.setattr(server.bitable, 'delete_records_by_prefix', fake_delete)
    result = server._cleanup_summary_rows_result()
    assert result['ok'] is True
    assert called['prefix'] == 'sum_'
    assert '删除 2 条' in result['reply']


def test_summary_reply_keeps_decimal_breakdowns():
    data = {
        'entries': [
            {'id': 'exp_1', 'type': 'expense', 'date': '2026-07-13', 'amount': 78.83, 'category': '买菜', 'payer': 'user', 'participants': ['user'], 'shares': {'user': 78.83}},
            {'id': 'exp_2', 'type': 'expense', 'date': '2026-07-12', 'amount': 499, 'category': '买菜', 'payer': 'user', 'participants': ['user'], 'shares': {'user': 499}},
            {'id': 'exp_3', 'type': 'expense', 'date': '2026-07-10', 'amount': 180, 'category': '住房', 'payer': 'user', 'participants': ['user'], 'shares': {'user': 180}},
            {'id': 'inc_1', 'type': 'income', 'date': '2026-07-09', 'amount': 6113.71, 'category': '收入', 'receiver': 'user'},
            {'id': 'inc_2', 'type': 'income', 'date': '2026-07-09', 'amount': 6000, 'category': '收入', 'receiver': 'user'},
        ]
    }
    reply = L.format_summary_reply(L.month_summary(data, '2026-07'))
    assert '收入: 12,113.71元' in reply
    assert '支出: 757.83元' in reply
    assert '结余: +11,355.88元' in reply
    assert '买菜 577.83元' in reply
    assert '住房 180.00元' in reply
    assert '收入 12,113.71元' in reply
    assert '个人账本，无需结算' in reply
    assert '买菜578' not in reply


def test_summary_uses_only_income_and_expense_types():
    data = {
        'entries': [
            {'id': 'inc_1', 'type': 'income', 'date': '2026-07-13', 'amount': 6000, 'category': '工资'},
            {'id': 'exp_1', 'type': 'expense', 'date': '2026-07-13', 'amount': 100, 'category': '买菜'},
            {'id': 'other_1', 'type': 'summary', 'date': '2026-07-13', 'amount': 5000, 'category': '结余'},
        ]
    }
    summary = L.month_summary(data, '2026-07')
    assert summary['income_total'] == 6000
    assert summary['expense_total'] == 100
    assert summary['net_total'] == 5900
    assert summary['ignored_count'] == 1
