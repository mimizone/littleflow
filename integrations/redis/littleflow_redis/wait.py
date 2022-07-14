import collections
import threading
from dataclasses import dataclass
from uuid import uuid4

from rqse import EventListener, message, receipt_for

@dataclass
class TaskInfo:
   workflow_id : str
   index : int
   name : str
   parameters : 'typing.Any' = None

class WaitForEventListener(EventListener):
   def __init__(self,key,parent,info,event_name,send_receipt=True,match={},server='0.0.0.0',port=6379,username=None,password=None,pool=None):
      super().__init__(key,f'wait-for-{uuid4()}',select=[event_name],server=server,port=port,username=username,password=password,pool=pool)
      self.parent = parent
      self._event_name = event_name
      self._info = info
      self._send_receipt = send_receipt
      self._match = match
      self._thread = None

   @property
   def target(self):
      f = lambda : self.listen()
      f.__name__ = f'wait_for {self._event_name} {self._match }'
      return f

   @property
   def thread(self):
      return self._thread

   @thread.setter
   def thread(self,value):
      self._thread = value

   def matches(self,event):
      for key,value in self._match.items():
         if event.get(key)!=value:
            return False
      return True

   def process(self,event_id, event):
      if not self.matches(event):
         return False
      if self._send_receipt:
         self.append(receipt_for(event_id))
      event = {'name':self._info.name,'index':self._info.index,'workflow':self._info.workflow_id}
      self.append(message(event,kind='end-task'))
      self.stop()
      if not self.parent.stop_work(self):
         print('Cannot remove wait_for thread.')
      return True

class Delay:
   def __init__(self,parent,info,delay):
      self.parent = parent
      self._info = info
      self._delay = delay
      self._wait = threading.Event()

   @property
   def target(self):
      f = lambda : self.run()
      f.__name__ = f'delay {self._delay}'
      return f

   @property
   def thread(self):
      return self._thread

   @thread.setter
   def thread(self,value):
      self._thread = value

   def stop(self):
      self._wait.set()

   def run(self):
      self._wait.wait(self._delay)
      event = {'name':self._info.name,'index':self._info.index,'workflow':self._info.workflow_id}
      self.parent.append(message(event,kind='end-task'))
      self.stop()
      if not self.parent.stop_work(self):
         print('Cannot remove delay thread.')
      return True

class WaitTaskListener(EventListener):

   def __init__(self,key,group='starting',server='0.0.0.0',port=6379,username=None,password=None,pool=None):
      super().__init__(key,group,select=['start-task'],server=server,port=port,username=username,password=password,pool=pool)
      self._work = collections.deque()
      self._lock = threading.RLock()

   def fail(self,workflow_id,index,name,reason=None):
      event = {'name':name,'index':index,'workflow':workflow_id,'status':'FAILURE'}
      if reason is not None:
         event['reason'] = reason
      self.append(message(event,kind='end-task'))
      return True

   def start_work(self,work):
      try:
         if not self._lock.acquire(timeout=30):
            return False
         work.thread = threading.Thread(target=work.target)
         self._work.append(work)
         work.thread.start()
         return True
      finally:
         self._lock.release()

   def stop_work(self,work):
      try:
         if not self._lock.acquire(timeout=30):
            return False
         self._work.remove(work)
         return True
      finally:
         self._lock.release()

   def wait_for(self,info,event_name,send_receipt=True,match={}):
      print(f'Workflow {info.workflow_id} is waiting for event {event_name}',flush=True)
      listener = WaitForEventListener(self._stream_key,self,info,event_name,send_receipt,match,pool=self.pool)
      if not self.start_work(listener):
         return False
      return True

   def delay(self,info,duration):
      print(f'Workflow {info.workflow_id} delay for {duration}',flush=True)
      do_delay = Delay(self,info,duration)
      if not self.start_work(do_delay):
         return False
      return True

   def onStop(self):
      for work in self._work:
         work.stop()

   def process(self,event_id, event):
      workflow_id = event.get('workflow')
      name = event.get('name')
      index = event.get('index')
      ns, _, task_name = name.partition(':')
      if ns!='wait':
         return False

      input = event.get('input')
      parameters = event.get('parameters')

      info = TaskInfo(workflow_id,index,name,input)

      self.append(receipt_for(event_id))

      if task_name=='delay':
         # TODO: parse units
         duration = parameters.get('duration')
         if duration is None:
            return self.fail(workflow_id,index,name,f'{task_name} does not have an duration parameter')
         duration = int(duration)
         if not self.delay(info,duration):
            return self.fail(workflow_id,index,name,f'Cannot acquire lock for {task_name} task')

      elif task_name=='event':
         event_name = parameters.get('event')
         if event_name is None:
            return self.fail(workflow_id,index,name,f'{task_name} does not have an event parameter')

         receipt = bool(parameters.get('receipt',True))

         match_kind = parameters.get('match',None)
         if match_kind=='input':
            if input is None:
               match = {}
            else:
               match = input if type(input)==dict else input[0]
         else:
            match = {}

         if not self.wait_for(info,event_name,receipt,match):
            return self.fail(workflow_id,index,name,f'Cannot acquire lock for {task_name} task')
      else:
         return self.fail(workflow_id,index,name,f'Unrecognized wait task name {task_name}')

      return True
