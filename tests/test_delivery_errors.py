from azusa_bot.delivery_errors import classify_send_error, safe_delivery_code


def test_bounded_error_classification():
    class ActionFailed(Exception):
        result = {'retcode':1200,'message':'private wording and secret'}
    class NetworkError(Exception):
        pass
    assert classify_send_error(ActionFailed()) == 'onebot_retcode_1200'
    ActionFailed.result['message'] = 'send timeout: private text'
    assert classify_send_error(ActionFailed()) == 'send_timeout'
    assert classify_send_error(NetworkError('WebSocket API call timeout')) == 'send_timeout'
    assert classify_send_error(ValueError('Invalid message identity')) == 'missing_receipt'
    assert safe_delivery_code('secret-from-upstream') == 'bridge_error'
