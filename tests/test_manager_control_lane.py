import os
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from modules.manager_cli import module as manager
from tools.vdmctl import send_request
from tests.test_manager_cli import FakeServer


def request(command,args):
    return {'protocol_version':1,'request_id':str(uuid.uuid4()),'issued_at_ms':int(time.time()*1000),'command':command,'arguments':args}


def dispose_after_requests(module,timeout=3.0):
    deadline=time.monotonic()+timeout
    while module.reload_blocked() and time.monotonic()<deadline:
        time.sleep(0.01)
    if module.reload_blocked():
        raise AssertionError('Named-pipe server did not drain its completed client')
    module.dispose()


def configured(action, fake=False, factory=None, *, status_reader=None, ui_action=None,
               process_image_checker=None,user_process_checker=None):
    options={'server_factory':factory} if factory else ({'server_factory':lambda h,**kw:FakeServer(h,**kw)} if fake else {})
    if process_image_checker is not None:options['process_image_checker']=process_image_checker
    if user_process_checker is not None:options['user_process_checker']=user_process_checker
    m=manager.ManagerCliModule(**options);m.init()
    m.bind_dependencies(status_reader=status_reader or (lambda:{}),topology_reader=lambda:{},pool_status=lambda:{'state':'off'},
        pool_begin=lambda p:{},pool_stop=lambda:{},task_action=action,window_reader=lambda:{},ui_action=ui_action)
    return m


class ManagerControlLaneTests(unittest.TestCase):
    def test_partial_start_cleanup_failure_retains_owned_listener(self):
        servers=[]
        class BrokenServer(FakeServer):
            def start(self):
                if self.kwargs['pipe_path']==manager.UI_CONTROL_PIPE_PATH:raise OSError('UI control bind failed')
                super().start()
            def stop(self):
                if self.ready:raise TimeoutError('listener still alive')
                super().stop()
        def factory(h,**kw):
            s=BrokenServer(h,**kw);servers.append(s);return s
        m=configured(lambda a,p:{},factory=factory)
        try:
            self.assertIs(m._server,servers[0])
            self.assertFalse(m._accepting)
            self.assertFalse(m.resume_after_shutdown())
        finally:
            for s in servers:s.ready=False
            m.dispose()

    def test_task_and_ui_control_lanes_reject_other_commands_and_account_for_listeners(self):
        m=configured(lambda a,p:{'state':'stopped'},True);self.addCleanup(m.dispose)
        state=m.refresh()
        self.assertTrue(state['control_pipe_ready'])
        self.assertTrue(state['ui_control_pipe_ready'])
        self.assertEqual(m._handle_control_request(request('task.start',{}))['error_code'],'CONTROL_COMMAND_UNSUPPORTED')
        self.assertEqual(m._handle_control_request(request('ui.status',{}))['error_code'],'CONTROL_COMMAND_UNSUPPORTED')
        self.assertEqual(m._handle_ui_control_request(request('task.status',{}))['error_code'],'UI_CONTROL_COMMAND_UNSUPPORTED')
        m._control_server.active_clients=1
        self.assertTrue(m.reload_blocked());self.assertFalse(m.pause_for_shutdown())
        m._control_server.active_clients=0
        m._ui_control_server.active_clients=1
        self.assertTrue(m.reload_blocked());self.assertFalse(m.pause_for_shutdown())
        m._ui_control_server.active_clients=0
        self.assertTrue(m.pause_for_shutdown());self.assertTrue(m.resume_after_shutdown())

    def test_stop_survives_normal_rate_and_nonce_saturation(self):
        m=configured(lambda a,p:{'state':'stopped'},True);self.addCleanup(m.dispose)
        m._request_times=[time.time()]*manager.MAX_REQUESTS_PER_MINUTE
        m._request_ids={str(uuid.uuid4()) for _ in range(manager.MAX_REQUEST_IDS)}
        result=m._handle_control_request(request('task.stop',{'session_id':str(uuid.uuid4())}))
        self.assertTrue(result['ok']);self.assertEqual(len(m._request_ids),manager.MAX_REQUEST_IDS)

    def test_ui_stop_survives_normal_rate_and_nonce_saturation(self):
        seen=[]
        m=configured(lambda a,p:{},True,ui_action=lambda action:seen.append(action) or {'accepted':True})
        self.addCleanup(m.dispose)
        m._request_times=[time.time()]*manager.MAX_REQUESTS_PER_MINUTE
        m._request_ids={str(uuid.uuid4()) for _ in range(manager.MAX_REQUEST_IDS)}
        result=m._handle_ui_control_request(request('ui.stop',{}))
        self.assertTrue(result['ok']);self.assertEqual(result['data'],{'accepted':True})
        self.assertEqual(seen,['stop'])
        self.assertEqual(len(m._request_ids),manager.MAX_REQUEST_IDS)

    @unittest.skipUnless(os.name=='nt','Real local Named Pipe fixture requires Windows')
    def test_real_control_pipe_reaches_stop_while_main_start_is_blocked(self):
        token=uuid.uuid4().hex
        main=rf'\\.\pipe\AIVDM_TestMain_{token}';control=rf'\\.\pipe\AIVDM_TestControl_{token}'
        ui_control=rf'\\.\pipe\AIVDM_TestUiControl_{token}'
        entered=threading.Event();released=threading.Event();stopped=threading.Event();sid=str(uuid.uuid4())
        results=[];errors=[];auth_checks=[]
        def image_check(pid,path):
            result=manager.process_image_matches(pid,path);auth_checks.append(('image',pid,str(path),result));return result
        def user_check(pid):
            result=manager.same_user_process(pid);auth_checks.append(('user',pid,result));return result
        def action(a,p):
            if a=='start':
                entered.set()
                if not released.wait(4):raise RuntimeError('test start gate expired')
                return {'state':'stopped','session_id':sid}
            if a=='stop':stopped.set();released.set();return {'state':'stopped','session_id':sid}
            return {'state':'running','active_session_id':sid,'session_state':'starting'}
        with patch.object(manager,'PIPE_PATH',main),patch.object(manager,'CONTROL_PIPE_PATH',control), \
                patch.object(manager,'UI_CONTROL_PIPE_PATH',ui_control):
            ui_action=lambda action:{'mode':'background_service','visible':False,'pending':None,
                                     'accepted':action!='status','last_error':None}
            m=configured(action,ui_action=ui_action,process_image_checker=image_check,
                         user_process_checker=user_check)
            def start():
                try:results.append(send_request('task.start',{'confirmed':True,'output_count':0,'profile':'private'},timeout_ms=8_000,pipe_path=main))
                except Exception as e:errors.append(e)
            t=threading.Thread(target=start);t.start()
            try:
                self.assertTrue(entered.wait(10),f'Main request never reached blocked start; worker_errors={errors!r}; auth_checks={auth_checks!r}; listeners={m.refresh()}')
                status=send_request('task.status',{},pipe_path=control)
                self.assertEqual(status['data']['active_session_id'],sid)
                result=send_request('task.stop',{'session_id':sid},pipe_path=control)
                self.assertTrue(result['ok']);self.assertTrue(stopped.is_set())
                ui=send_request('ui.status',{},pipe_path=ui_control)
                self.assertTrue(ui['ok'])
                t.join(2);self.assertFalse(t.is_alive());self.assertEqual(errors,[])
                self.assertEqual(results[0]['data']['state'],'stopped')
            finally:
                released.set();t.join(10);dispose_after_requests(m)

    @unittest.skipUnless(os.name=='nt','Real local Named Pipe fixture requires Windows')
    def test_real_ui_control_pipe_remains_responsive_while_main_status_is_blocked(self):
        token=uuid.uuid4().hex
        main=rf'\\.\pipe\AIVDM_TestMain_{token}';control=rf'\\.\pipe\AIVDM_TestControl_{token}'
        ui_control=rf'\\.\pipe\AIVDM_TestUiControl_{token}'
        entered=threading.Event();released=threading.Event();results=[];errors=[];auth_checks=[]
        def image_check(pid,path):
            result=manager.process_image_matches(pid,path);auth_checks.append(('image',pid,str(path),result));return result
        def user_check(pid):
            result=manager.same_user_process(pid);auth_checks.append(('user',pid,result));return result
        def slow_status():
            entered.set()
            if not released.wait(4):raise TimeoutError('test status gate expired')
            return {'state':'ok'}
        ui_action=lambda action:{'mode':'background_service','visible':False,'pending':None,
                                 'accepted':action!='status','last_error':None}
        with patch.object(manager,'PIPE_PATH',main),patch.object(manager,'CONTROL_PIPE_PATH',control), \
                patch.object(manager,'UI_CONTROL_PIPE_PATH',ui_control):
            m=configured(lambda a,p:{'state':'stopped'},status_reader=slow_status,ui_action=ui_action,
                         process_image_checker=image_check,user_process_checker=user_check)
            def status():
                try:results.append(send_request('status',{},timeout_ms=8_000,pipe_path=main))
                except Exception as e:errors.append(e)
            t=threading.Thread(target=status);t.start()
            try:
                self.assertTrue(entered.wait(10),f'Main status handler did not block; worker_errors={errors!r}; auth_checks={auth_checks!r}; listeners={m.refresh()}')
                response=send_request('ui.status',{},pipe_path=ui_control)
                self.assertTrue(response['ok'])
                self.assertEqual(response['data']['mode'],'background_service')
                self.assertTrue(t.is_alive(),'The main status path unexpectedly unblocked')
            finally:
                released.set();t.join(10);dispose_after_requests(m)
            self.assertFalse(t.is_alive());self.assertEqual(errors,[])
            self.assertEqual(results[0]['data']['state'],'ok')


if __name__=='__main__':unittest.main()



