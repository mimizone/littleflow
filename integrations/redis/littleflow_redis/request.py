import logging

import requests 

from rqse import EventListener, message, receipt_for
from littleflow import merge

from .context import RedisOutputCache

def value_for(input,parameters,name,default=None):
   return input.get(name,parameters.get(name,default)) if input is not None else parameters.get(name,default) if parameters is not None else default

class RequestTaskListener(EventListener):

   def __init__(self,key,credential_actor=None,group='request',host='0.0.0.0',port=6379,username=None,password=None,pool=None):
      super().__init__(key,group,select=['start-task'],host=host,port=port,username=username,password=password,pool=pool)
      self._credential_actor = credential_actor

   def fail(self,workflow_id,index,name,reason=None):
      event = {'name':name,'index':index,'workflow':workflow_id,'status':'FAILURE'}
      if reason is not None:
         event['reason'] = reason
      self.append(message(event,kind='end-task'))
      return True

   def output_for(self,workflow_id,index,output):
      if output is not None:
         cache = RedisOutputCache(self.connection,workflow_id)
         cache[index] = output

   def process(self,event_id, event):

      is_debug = logging.DEBUG >= logging.getLogger().getEffectiveLevel()

      workflow_id = event.get('workflow')
      name = event.get('name')
      index = event.get('index')
      ns, _, task_name = name.partition(':')
      if ns!='request':
         return False

      input = event.get('input')
      parameters = event.get('parameters')

      self.append(receipt_for(event_id))

      sync = bool(value_for(input,parameters,'sync',True))
      url = value_for(input,parameters,'url')
      template = value_for(input,parameters,'template')
      content_type = value_for(input,parameters,'content_type','application/json')
      use_context_parameters = bool(value_for(input,parameters,'use_context_parameters',True))
      output_modes = value_for(input,parameters,'output_mode',[])
      if type(output_modes)==str:
         output_modes = [output_modes]
      error_on_status = bool(value_for(input,parameters,'error_on_status',True))

      if task_name not in ['get','post','put','delete']:
         return self.fail(workflow_id,index,name,f'Unrecognized wait task name {task_name}')

      if url is None:
         return self.fail(workflow_id,index,name,f'Unrecognized wait task name {task_name}')

      if not sync and use_context_parameters:
         if url.rfind('?')<0:
            url += '?'
         else:
            url += '&'
         url += f'task-name={name}'
         url += f'&index={index}'
         url += f'&workflow={workflow_id}'

      if is_debug:
         logging.debug(f'HTTP {task_name.upper()} request on {url}')

      try:
         headers = {}
         if self._credential_actor is not None:
            headers['Authorization'] = f'Bearer {self._credential_actor(input,parameters)}'
            if is_debug:
               logging.debug(f'Authorization: {headers["Authorization"]}')
         data = template.format(input=input,parameters=parameters) if template is not None else None
         if task_name=='get':
            response = requests.get(url,headers=headers)
         elif task_name=='post':
            headers['Content-Type'] = content_type
            response = requests.post(url,headers=headers,data=data if data is not None else '')
         elif task_name=='put':
            headers['Content-Type'] = content_type
            response = requests.put(url,headers=headers,data=data if data is not None else '')
         elif task_name=='delete':
            response = requests.delete(url)

         if is_debug:
            logging.debug(f'{response.status_code} response for {url}')

         if error_on_status and (response.status_code<200 or response.status_code>=300):
            return self.fail(workflow_id,index,name,f'Request failed ({response.status_code}): {response.text}')


         if sync:
            try:
               output = input
               for mode in output_modes:
                  if output is None:
                     output = {}
                  if mode=='status':
                     output['request_status'] = response.status_code
                  elif mode=='response_text':
                     if is_debug:
                        logging.debug(response.text)
                     output['response'] = response.text
                  elif mode=='response_json':
                     if is_debug:
                        logging.debug(response.text)
                     output['response'] = response.json()
            except Exception as ex:
               logging.exception(f'Unabled to process output of {name} due to exception.')
               return self.fail(workflow_id,index,name,f'Unabled to process response output due to exception: {ex}')

            self.output_for(workflow_id,index,output)

            event = {'name':name,'index':index,'workflow':workflow_id}
            self.append(message(event,kind='end-task'))

         return True

      except Exception as ex:
         logging.exception(f'Unabled to send {name} due to exception.')
         return self.fail(workflow_id,index,name,f'Unabled to send request due to exception: {ex}')
