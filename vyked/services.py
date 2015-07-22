from asyncio import Future, get_event_loop, coroutine, iscoroutinefunction
import asyncio
from functools import wraps

from again.utils import unique_hex
from aiohttp.web import Response

from .packet import MessagePacket
from .exceptions import RequestException
from .utils.ordered_class_member import OrderedClassMembers


class _Service:
    _PUB_PKT_STR = 'publish'
    _REQ_PKT_STR = 'request'
    _RES_PKT_STR = 'response'

    def __init__(self, service_name, service_version):
        self._service_name = service_name
        self._service_version = service_version
        self._bus = None
        self._service_clients = []

    @property
    def name(self):
        return self._service_name

    @property
    def version(self):
        return self._service_version

    @property
    def properties(self):
        return self.name, self.version

    @property
    def bus(self):
        return self._bus

    @bus.setter
    def bus(self, bus):
        self._bus = bus

    @staticmethod
    def time_future(future:Future, timeout:int):
        def timer_callback(f):
            if not f.done() and not f.cancelled():
                f.set_exception(TimeoutError())

        get_event_loop().call_later(timeout, timer_callback, future)

    def require(self, clients):
        for client in clients:
            if isinstance(client, (TCPServiceClient, HTTPServiceClient)):
                client.bus = self
                self._service_clients.append(client)


class TCPServiceClient(_Service):
    REQUEST_TIMEOUT_SECS = 10

    def __init__(self, service_name, service_version):
        super(TCPServiceClient, self).__init__(service_name, service_version)
        self._pending_requests = {}

    def _send_request(self, app_name, endpoint, entity, params):
        packet = MessagePacket.request(self.name, self.version, app_name, _Service._REQ_PKT_STR, endpoint, params, entity)
        future = Future()
        request_id = params['request_id']
        self._pending_requests[request_id] = future
        self._bus.send(packet)
        _Service.time_future(future, TCPServiceClient.REQUEST_TIMEOUT_SECS)
        return future

    def receive(self, packet: dict, protocol, transport):
        _logger.info('service client {}, packet {}'.format(self, packet))
        _logger.info('active pingers {}'.format(self._pingers))
        if packet['type'] == 'ping':
            self._handle_ping(packet['node_id'], packet['count'])
        elif packet['type'] == 'pong':
            pinger = self._pingers[packet['node_id']]
            asyncio.async(pinger.pong_received(packet['count']))
        else:
            self._process_response(packet)

    def process_packet(self, packet):
        if packet['type'] == _Service._RES_PKT_STR:
            self._process_response(packet)
        elif packet['type'] == _Service._PUB_PKT_STR:
            self._process_publication(packet)
        else:
            print('Invalid packet', packet)

    def _process_response(self, packet):
        payload = packet['payload']
        request_id = payload['request_id']
        has_result = 'result' in payload
        has_error = 'error' in payload
        future = self._pending_requests.pop(request_id)
        if has_result:
            future.set_result(payload['result'])
        elif has_error:
            exception = RequestException()
            exception.error = payload['error']
            future.set_exception(exception)
        else:
            print('Invalid response to request:', packet)

    def _process_publication(self, packet):
        endpoint = packet['endpoint']
        func = getattr(self, endpoint)
        func(**packet['payload'])



class _ServiceHost(_Service):
    def __init__(self, service_name, service_version, host_ip, host_port):
        super(_ServiceHost, self).__init__(service_name, service_version)
        self._ip = host_ip
        self._port = host_port

    def is_for_me(self, service, version):
        return service == self.name and int(version) == self.version

    @property
    def socket_address(self):
        return self._ip, self._port


class TCPService(_ServiceHost):
    def __init__(self, service_name, service_version, host_ip, host_port):
        # TODO: to be multi-tenant make app_name a list
        super(TCPService, self).__init__(service_name, service_version, host_ip, host_port)

    def _publish(self, endpoint, payload):
        self._bus.publish(self.name, self.version, endpoint, payload)

    def _make_response_packet(self, request_id: str, from_id: str, entity: str, result: object, error: object):
        if error:
            payload = {'request_id': request_id, 'error': error}
        else:
            payload = {'request_id': request_id, 'result': result}

        packet = {'pid': unique_hex(),
                  'to': from_id,
                  'entity': entity,
                  'type': _Service._RES_PKT_STR,
                  'payload': payload}
        return packet


class RequestException(Exception):
    pass


def _default_preflight_response(self, request):
    return Response(status=200,
                    headers={'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET,POST,PUT',
                             'Access-Control-Allow-Headers': 'accept, content-type'})

class HTTPService(_ServiceHost, metaclass=OrderedClassMembers):
    def __init__(self, service_name, service_version, host_ip, host_port, ssl_context=None, allow_cross_domain=False,
                 preflight_response=_default_preflight_response):
        super(HTTPService, self).__init__(service_name, service_version, host_ip, host_port)
        self._ssl_context = ssl_context
        self._allow_cross_domain = allow_cross_domain
        self._preflight_response = preflight_response

    @property
    def ssl_context(self):
        return self._ssl_context

    @property
    def cross_domain_allowed(self):
        return self._allow_cross_domain

    @property
    def preflight_response(self):
        return self._preflight_response

    def pong(self, request):
        return Response()


class HTTPServiceClient(_Service):
    def __init__(self, service_name, service_version):
        super(HTTPServiceClient, self).__init__(service_name, service_version)

    def _send_http_request(self, app_name, method, entity, params):
        response = yield from self._bus.send_http_request(app_name, self.name, self.version, method, entity,
                                                          params)
        return response
