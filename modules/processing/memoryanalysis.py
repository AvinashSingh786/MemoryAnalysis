import copy
import memoryanalysisconsts
import os			
import re
import tempfile
import shutil
import pydeep
import time
import json
import logging

from lib.cuckoo.common.config import Config
from lib.cuckoo.common.constants import CUCKOO_ROOT
from modules.processing.static import PortableExecutable

import modules.processing.memory
import modules.processing.virustotal as vt

from volatility.plugins import volshell
from volatility import utils
import volatility.plugins.vadinfo as vadinfo

import distorm3
import win32con

log = logging.getLogger(__name__)


def disasm_from_memory(memory_dump_path, pid, base_address, memory_len):
	data = get_memory_from_proc(memory_dump_path, pid, base_address, memory_len)
	iterable = distorm3.DecodeGenerator(base_address, data, distorm3.Decode32Bits)
	ret = ""
        for (offset, _size, instruction, hexdump) in iterable:
                ret += "{0:<#8x} {1:<32} {2}".format(offset, hexdump, instruction)
	return ret

def get_memory_from_proc(memory_dump_path, pid, base_address, memory_len):
        """
        Gets a given length of memory from a dump file from a given PID.
        Returns None if the memory was unreadable.
        """
        # We use volatility API just in order to get it to set our config correctly
        m = modules.processing.memory.VolatilityAPI(memory_dump_path)
        v = volshell.volshell(m.config)
        v.addrspace = m.addr_space
        v.set_context(pid=pid)
        return v.proc.get_process_address_space().read(base_address, memory_len)


def add_static_to_malfind(res):
	res2 = []
        for i in res:
        	if i["data"].startswith("MZ".encode("hex")):
                	# Write file
                        data = i["data"].decode("hex")
                        temp_f = tempfile.mktemp()
                        file(temp_f,"wb").write(data)
                        i["static"] = PortableExecutable(temp_f).run()
                        os.remove(temp_f)
                res2.append(i)
        return res2

class hashabledict(dict):
    """
    Class that enables dictionary sets.
    """
    def __hash__(self):
        	return hash(tuple(sorted(self.items())))

class MemoryAnalysis(object):
	"""
	Class for Memory analysis diff logics.
	"""
	def __init__(self, voptions, vol, memfile, file_path, is_clean, clean_data, clean_dump):
		self.voptions = voptions
		self.vol_manager = vol
		
		self.results = vol.results
		self.memfile = memfile
		self.file_path = file_path
		self.is_clean = is_clean
		self.clean_data = clean_data
		self.clean_dump = clean_dump
		if not self.is_clean:
			self.vol_manager = copy.deepcopy(vol)
		self.clean_dump = clean_dump
		dump_dir = os.path.dirname(self.memfile)
		self.info = None
		info_json_file = os.path.join(dump_dir, "info.json")
		if os.path.exists(info_json_file):
			self.info = json.load(file(info_json_file, "rb"))
	def run_dependencies(self, results, dependencies, **attrs):
		"""
		Runs each dependency if its result does not already exist  
		"""
		for dep in dependencies:
			if not results.has_key(dep):
				results[dep] = getattr(self.vol_manager, dep)(**attrs)
		return results

	def get_new_objects(self, old, new, deps):
		"""
		Gets the diff between the old dump and new dump objects.
		"""
		hashable_united_old = set([])
		hashable_united_new = set([])
		for dep in deps:
			hashable_united_old = hashable_united_old | set([hashabledict(d) for d in old[dep]['data']])
			hashable_united_new = hashable_united_new | set([hashabledict(d) for d in new[dep]['data']])
		return list(set(hashable_united_new) - set(hashable_united_old))

	def get_new_dlllist(self, old, new, deps):
		new_dlls = []
		for proc in new['dlllist']['data']:
			pid = proc['process_id']
			pname = proc['process_name']
			old_p = None
			for p in old['dlllist']['data']:
				if pid == p['process_id']:
					old_p = p
					break
			if old_p is not None:
				for module in proc['loaded_modules']:
					found = False
					for m in old_p['loaded_modules']:
						if m['dll_full_name'] == module['dll_full_name']:
							found = True
							break
					if not found:
						module['process_id'] = pid
						module['process_name'] = pname
						new_dlls.append(module)
		return new_dlls
	
	def get_new_by_field(self, old, new, deps, fields):

		found_objs = self.get_new_objects(old, new, deps)
		new_objs = []
		
		for o in found_objs:
			exist = False
			for o2 in old[deps[0]]['data']:
				similar = True
				for f in fields:
					if not o[f] == o2[f]:
						similar = False
						break
				if similar:
					exist = True
					break
			if not exist:
				new_objs.append(o)

		return new_objs

	def get_new_handles(self, old, new, deps):
		return self.get_new_by_field(old,new,deps,['process_id', 'handle_value'])
	def get_new_timers(self, old, new, deps):
		return self.get_new_by_field(old, new, deps, ['offset'])

	def get_new_mutants(self, old, new, deps):
		return self.get_new_by_field(old, new, deps, ['mutant_name'])

	def get_new_connections(self, old, new, deps):
		found_connections = self.get_new_objects(old, new, deps)
		new_connections = []
		for c in found_connections:
			if not (c["local_port"] == 8000 or \
			   c['remote_port'] == 2042 or \
			   c["remote_address"]	== "293.268.122.1"):
				new_connections.append(c)
		return new_connections

	def get_new_malfind(self, old, new, deps, dump_dir=None):
		found_mals = self.get_new_objects(old, new, deps)
		new_mals = []
		malfinds_dir = os.path.join(os.path.dirname(self.memfile), "malfinds//")
		for m in found_mals:
			if not (m["process_name"] == "python.exe"):
				new_mals.append(m)
		# Copy new drivers to dir
		if dump_dir is not None:
                	for new_mal in new_mals:
				name = "process.{0:#x}.{1:#x}.dmp".format(new_mal["offset"], int(new_mal["vad_start"],16))
                        	#shutil.copy(new["malfind"]["config"]["dump_dir"] + '//' + name, malfinds_dir + name)
                        	try:
					shutil.copy(dump_dir + '//' + name, malfinds_dir + name)
				except:
					pass
                	try:
                        	shutil.rmtree(old["malfind"]["config"]["dump_dir"])
                        	shutil.rmtree(new["malfind"]["config"]["dump_dir"])
                	except:
                        	pass

		return new_mals


	def get_new_processes(self, old, new, deps):
		"""
		Gets new processes between old and new dumps.
		"""
		return self.get_new_by_field(old, new, deps, ['process_id'])

	def get_new_services(self, old, new, deps):
		return self.get_new_by_field(old, new, deps, ['service_name'])

	def get_new_devices(self, old, new, deps):
		old_drivers = old[deps[0]]['data']
		new_drivers = new[deps[0]]['data']
		found_devs = []
		for new_driver in new_drivers:
			new_devices = new_driver["devices"]
			for new_device in new_devices:
				is_new = True
				for old_driver in old_drivers:
					old_devices = old_driver["devices"]
					for old_device in old_devices:
						if old_device["device_name"] == new_device["device_name"] and old_device["device_type"] == new_device["device_type"]:
							is_new = False
							break
					if not is_new:
						break
				if is_new:
					found_devs.append(new_device)
		return found_devs
	def get_new_hidden_processes(self, old, new, deps):
		"""
		Finds hidden processes
		"""
		proclist = self.get_new_processes(old, new, deps)
		hidden = []
		for proc in proclist:
			for rule in memoryanalysisconsts.PSXSCAN_RULES:
				should_add = True
				for (attr, val) in rule.iteritems():
					if proc[attr] != val:
						should_add = False
						break
				# Avoid adding cuckoo's hidden process
				if should_add and proc['process_name'] != "python.exe":
					hidden.append(proc)
					break
		return hidden		
	def get_new_moddump(self, old, new, deps):
		moddir = os.path.join(os.path.dirname(self.memfile), "drivers//")
                res = self.get_new_objects(old, new, deps)

                # Copy new drivers to dir
                for new_driver in res:
			try:
				shutil.copy(new["moddump"]["config"]["dump_dir"] + "/driver.{0:x}.sys".format(new_driver["module_base"]), moddir + new_driver["module_name"])
			except:
				pass
		try:
			shutil.rmtree(old["moddump"]["config"]["dump_dir"])
			shutil.rmtree(new["moddump"]["config"]["dump_dir"])
		except:
			pass
		return res
	def get_autostart_reg_keys(self, old, new, deps):
		autostart = []
		for new_handle in new[deps[0]]['data']:
			is_autostart = False
			if new_handle['handle_type'] == 'Key':
				for k in memoryanalysisconsts.AUTOSTART_REG_KEYS:
					if k in new_handle["handle_value"].lower():
						is_autostart = True
						break
				if is_autostart:
					autostart.append(new_handle)
		return autostart

	def get_functions_as(self, p):
		d = {}
		for func_offset in p.names.keys():
			if p.names[func_offset].startswith('sub'):
				d[p.names[func_offset]] = p.getBytes(func_offset, p.getFunctionEnd(func_offset) - func_offset)
		return d

	def get_malware_proc(self, old, new, deps):
		processes = new['psxview']['data']
                mal_exe_proc = None
                for new_proc in processes:
                        if "m.exe" in new_proc["process_name"]:
                                mal_exe_proc = new_proc
                                break
		return mal_exe_proc

	def static_analyze_new_exe(self, old, new, deps, return_data=False):
		mal_exe_proc = self.get_malware_proc(old,new,deps)
		if mal_exe_proc is None:
			return []
		dump_dir = os.path.dirname(self.memfile)
		vol = modules.processing.memory.VolatilityAPI(self.memfile)
		vol.procexedump(dump_dir=dump_dir, pid=str(mal_exe_proc["process_id"]))
		if len(os.listdir(dump_dir)) > 1:
			new_exe = None
			for f in os.listdir(dump_dir):
				if f.endswith(".exe"):
					new_exe = f
					break
			if new_exe == None:
				return []
			new_exe_full_path = os.path.join(dump_dir,new_exe)
			static = PortableExecutable(new_exe_full_path).run()
			file_data = file(new_exe_full_path, "rb").read()
			static["pid"] = mal_exe_proc["process_id"]
			try:
				os.remove(r'home/tomer/bla.txt')
			except:
				pass
			command = "wine /home/tomer/.wine/drive_c/Program\ Files\ \(x86\)/IDA\ Free/idag_patched.exe -A -S'/home/tomer/cuckoo/IDA/script.idc' '%s' &" % new_exe_full_path
            		os.system(command)
            		time.sleep(30)
            		os.system("pkill idag_patched")
            		pefuncs = [a.split("^") for a in file(r'/home/tomer/bla.txt','rb').read().split("--------------\r\n")]
			funcs = []
			try:
                		funcs.remove([""])
            		except:
                		pass

            		for func in pefuncs:
                		buf = ""
                		if len(func) == 3:
                        		for i in func[2]:
                                		try:
                                        		i.encode('utf-8')
                                        		buf += i
                                		except UnicodeDecodeError:
                                   	     		pass
                        		funcs.append({'name':func[0], 'data':func[1], 'disassembly':buf})
			static["pe_functions"] = funcs
			#shutil.rmtree(temp_dir)
			static["path"] = new_exe_full_path
			if not return_data:
				return [static]
			else:
				return [static], file_data
		#shutil.rmtree(temp_dir)
		return []


	def get_new_pe_objects(self, object_type, static_from_exe, static_from_memory):
		matched_obs = []
		new_obs = []
		deleted_obs = []

		for old_ob in static_from_exe[object_type]:
			ob_name = old_ob["name"]
			is_matched = False
			for new_ob in static_from_memory[object_type]:
				if new_ob["name"] == ob_name:
					matched_obs.append([old_ob, new_ob])
					is_matched = True
					break
			if not is_matched:
				deleted_obs.append(old_ob)
		new_matched_obs = [ob[1] for ob in matched_obs]
		for new_ob in static_from_memory[object_type]:
			if not new_ob in new_matched_obs:
				new_obs.append(new_ob)

		return matched_obs, new_obs, deleted_obs

	def diff_static_analysis(self, old, new, deps, show_new_imports=False):
		results = {}
		
		#conf = Config(os.path.join(CUCKOO_ROOT, "conf", "memory.conf"))
		#show_new_imports = conf.diff_static_analysis.show_new_imports	
		res = self.static_analyze_new_exe(old, new, deps, True)
		if len(res) == 0:
			return results
		else:
			static_from_memory, new_exe_data = res
		static_from_memory = static_from_memory[0]
		
		# Calculate for exe
		if old == []:
			old = PortableExecutable(self.file_path).run()

		matched_sections, new_sections, deleted_sections = self.get_new_pe_objects("pe_sections", old, new)
		matched_resources, new_resources, deleted_resources = self.get_new_pe_objects("pe_resources", old, new)
		

		# Check changed entropy & ssdeep in sections and resources
		results["sections"] = {}
		#results["sections"]["num_of_sections"] = (len(static_from_exe["pe_sections"]), \
		#					     len(static_from_memory["pe_sections"]))		
		results["sections"]["new_sections"] = new_sections
		results["sections"]["deleted_sections"] = deleted_sections
		#results["sections"]["matched_sections"] = matched_sections
		#results["sections"]["sections"] = []
		"""
		for old_section, new_section in matched_sections:
			sec = {"name" : old_section["name"]}
			sec["entropy"] = (old_section["entropy"], new_section["entropy"])
			ssdeep1 = old_section["ssdeep"]
			ssdeep2 = new_section["ssdeep"]
			sec["ssdeep"] = {"values" : (ssdeep1, ssdeep2), 
					 "compare" : pydeep.compare(ssdeep1, ssdeep2)}
			sec["size"] = (old_section["size_of_data"], new_section["size_of_data"])
			results["sections"]["sections"].append(sec)
		"""
		results["resources"] = {}
		#results["resources"]["num_of_resources"] = (len(static_from_exe["pe_resources"]), \
						            #len(static_from_memory["pe_resources"]))
		results["resources"]["new_resources"] = new_resources
		results["resources"]["deleted_resources"] = deleted_resources
		#results["resources"]["matched_resources"] = matched_resources
		#results["resources"]["resources"] = []
		"""
		for old_resource, new_resource in matched_resources:
			res = {"name" : old_resource["name"]}
			res["entropy"] = (old_resource["entropy"], new_resource["entropy"])
			ssdeep1 = old_resource["ssdeep"]
			ssdeep2 = new_resource["ssdeep"]
			res["ssdeep"] = {"values" : (ssdeep1, ssdeep2), 
					 "compare" : pydeep.compare(ssdeep1, ssdeep2)}
			res["size"] = (old_resource["size"], new_resource["size"])
			results["resources"]["resources"].append(res)
		"""
		#results["entry_point"] = hex(static_from_memory["pe_entry_point"])
		#results["number_of_exports"] = static_from_memory["pe_exports"]
		#vol = modules.processing.memory.VolatilityAPI(self.memfile)
		#imps = vol.impscan(pid=static_from_memory["pid"])["data"]
		#if show_new_imports:
		#	results["new_imports"] = imps
		#results["number_of_imports"] = (len(static_from_exe["pe_imports"]), len(imps))
		#tempf = tempfile.mktemp()
		#file(tempf, "wb").write(new_exe_data)
		#vt1 = vt.run_vt_analysis_on_file(self.file_path)
		#vt2 = vt.run_vt_analysis_on_file(tempf)
		#if not vt2.has_key("scans"):
			#json_res = vt.send_file_for_scan(tempf)
			#while (True)
		# TODO: send file for scan in VT
		#results["vt_ratio"] = (str(vt1["positives"]) +  '/' + str(vt1["total"]),
				       #"-")
		#os.remove(tempf)

		return dict(sorted(results.iteritems(), key=lambda key_value: key_value[0]))
	def parse_trigger_plugins(self, trigger):
		smart_analysis = False
		plugins = []
		conf = Config(os.path.join(os.path.join(CUCKOO_ROOT, "conf", "triggers.conf")))
		if hasattr(conf, trigger):
			trig_opts = getattr(conf, trigger)
			if trig_opts.run_smart_plugins:
				smart_analysis = True
			if trig_opts.run_plugins != "":
				plugins = trig_opts.run_plugins.split(",")
		return smart_analysis, plugins

	def run_memory_plugin(self, mem_analysis, memfunc, clean, new_results, **attrs):
		conf = Config(os.path.join(CUCKOO_ROOT, "conf", "memory.conf"))
		desc = getattr(conf, memfunc).desc
		deps = memoryanalysisconsts.MEMORY_ANALYSIS_DEPENDENCIES[memfunc]
		if memoryanalysisconsts.MEMORY_ANALYSIS_FUNCTIONS.has_key(memfunc):
                	analysis_func = getattr(self, memoryanalysisconsts.MEMORY_ANALYSIS_FUNCTIONS[memfunc])
                else:
                        analysis_func = self.get_new_objects
		if not mem_analysis["diffs"].has_key(memfunc):
                        mem_analysis["diffs"][memfunc] = {"desc" : desc}
			mem_analysis["diffs"][memfunc]["new"] = analysis_func(clean, new_results, deps, **attrs)
			mem_analysis["diffs"][memfunc]["deleted"] = analysis_func(new_results, clean, deps, **attrs)
			mem_analysis["diffs"][memfunc]["star"] = "no"


	def run_memory_analysis(self):		
		"""
		The main engine function for memory analysis
		"""
		# We copy the results because we add results that the user did not choose to run,
		# but will run as a dependency.
		log.info("Running memory analysis for dump: %s" % self.memfile)
		new_results = copy.deepcopy(self.results)
		mem_analysis = {}
		mem_analysis["diffs"] = {}
                vol = modules.processing.memory.VolatilityAPI(self.memfile)
		dlls_dir = os.path.join(os.path.dirname(self.memfile), "dlls")
                drivers_dir = os.path.join(os.path.dirname(self.memfile), "drivers")
                malfinds_dir = os.path.join(os.path.dirname(self.memfile), "malfinds")

		if not self.is_clean:
			try:
                         	os.mkdir(dlls_dir)
				os.mkdir(drivers_dir)
				os.mkdir(malfinds_dir)
                        except:
                        	pass

        	for mem_analysis_name, dependencies in memoryanalysisconsts.MEMORY_ANALYSIS_DEPENDENCIES.iteritems():
			attrs = getattr(self.voptions, mem_analysis_name)
			if attrs.enabled:
				log.info("Running plugin: %s" % mem_analysis_name)
				attrs.pop("enabled")
				desc = attrs.pop("desc")
				try:
					attrs.pop("filter")
				except:
					pass
				if self.is_clean:
                                	self.results = self.run_dependencies(self.results, dependencies, **attrs)
                        	else:
                                	new_results = self.run_dependencies(new_results, dependencies, **attrs)
					if mem_analysis_name == "static_analyze_new_exe":
						#mal_exe_proc = self.get_malware_proc(old,new,deps)
						mem_analysis[mem_analysis_name] = self.static_analyze_new_exe(self.clean_data, new_results, dependencies, **attrs)
					elif mem_analysis_name == "diff_heap_entropy":
						mem_analysis["diffs"]["diff_heap_entropy"] = {"desc" : "Calculates entropy of malware heap process", "new" : [], "deleted" : [], "star": "no"}
						proc = self.get_malware_proc(self.clean_data,new_results,dependencies)
                				if proc is not None:
                        				pid = proc["process_id"]
							try:
                        					mem_analysis["diffs"]["diff_heap_entropy"]["value"] = vol.heapentropy(pid=str(pid))["data"][0]["entropy"]
							except:
								pass
					else:
						self.run_memory_plugin(mem_analysis, mem_analysis_name, self.clean_data, new_results, **attrs)
		if not self.is_clean:
			trigger = self.info["trigger"]["name"]
			args = self.info["trigger"]["args"]
			smart_analysis, plugins_to_run = self.parse_trigger_plugins(trigger)
			mem_analysis["diffs"]["injected_thread"] = {"new":[],"deleted":[],"star":"no", "desc":"Shows the injected thread"}
			mem_analysis["diffs"]["injected_dll"] = {"star" : "no", "new": [], "deleted": [], "desc" : "Finds the injected dll and dumps it"}
			vol = modules.processing.memory.VolatilityAPI(self.memfile)
			clean_vol = modules.processing.memory.VolatilityAPI(self.clean_dump)


		#if plugins_to_run != []:
		#	for plugin in plugins_to_run:
		#		run_simple_plugin(mem_analysis, plugin)
			if smart_analysis:
			    if memoryanalysisconsts.TRIGGER_PLUGINS.has_key(trigger):
				for plugin in memoryanalysisconsts.TRIGGER_PLUGINS[trigger]:
					#self.run_memory_plugin(mem_analysis, plugin, self.clean_data, new_results)
					if mem_analysis["diffs"].has_key(plugin):
						mem_analysis["diffs"][plugin]["star"] = "yes"

			    if trigger == "monitorCPU":
				if mem_analysis["diffs"].has_key("diff_heap_entropy"):
					mem_analysis["diffs"]["diff_heap_entropy"]["star"] = "yes"
		    	    if trigger == "ZwLoadDriver":
				patcher_res = vol.patcher("/home/tomer/cuckoo/patchpe.xml")
				# Dump the new driver

				for p in patcher_res["patches"]:
					log.info("Dumping drive in offset: %d" % p)
					base = int(p) + 0x7fe00000
					res = vol.moddump(dump_dir=drivers_dir, base=base)
					name = res["data"][0]["module_name"]
					mem_analysis["diffs"]["diff_moddump"]["new"].append({"module_name" : name,"module_base" : base})
			    elif trigger == "SetWindowsHookExA" or trigger == "SetWindowsHookExW":
				if mem_analysis["diffs"].has_key("diff_messagehooks"):
					for mh in mem_analysis["diffs"]["diff_messagehooks"]["new"]:
						pid_pat = "\(.*?(\d+)\)"
						
						pids = re.findall(pid_pat, mh["thread"])
						if len(pids) > 0:
							pid = pids[0]
							mod_name = mh["module"].split("\\")[-1]
							dlldump = vol.dlldump(dump_dir=dlls_dir, pid=pid, regex=mod_name)
							if dlldump["data"] != []:
								mem_analysis["diffs"]["injected_dll"]["new"].append(dlldump["data"][0])	
								mem_analysis["diffs"]["injected_dll"]["star"] = "yes"
			    elif trigger == "VirtualProtectEx":
				#protect = vadinfo.PROTECT_FLAGS.get(int(args["Protection"],16), "")
				verbal_protect = memoryanalysisconsts.PAGE_PROTECTIONS[int(args["Protection"],16)]
				protect_vad_val = [key for key,val in vadinfo.PROTECT_FLAGS.iteritems() if val == verbal_protect][0]
        			if "EXECUTE" in verbal_protect:
					# Fix protection
					# Run malfind and mark as star
					mem_analysis["diffs"]["diff_malfind"]["new"].append(vol.malfind(dump_dir=malfinds_dir, pid=str(args["ProcessId"]), add_data=False, ignore_protect=True, address=int(args["Address"],16))["data"][0])
					#self.run_memory_plugin(mem_analysis, "new_malfind", self.clean_data, new_results)	
				else:				
					mem_analysis["diffs"]["diff_malfind"]["deleted"].append(vol.malfind(dump_dir=None, pid=str(args["ProcessId"]), add_data=False, ignore_protect=True, address=int(args["Address"],16))["data"][0])
				mem_analysis["diffs"]["diff_malfind"]["star"] = "yes"
		    	    elif trigger == "WriteProcessMemory -> CreateRemoteThread -> LoadLibrary":
			 	resdir = os.path.join(os.path.dirname(self.memfile), "dlls")
                                try:
                                	os.mkdir(resdir)
                                except:
                                        pass	
				pid = str(args["ProcessId"])
				base = int(args["BaseAddress"], 16)
				
				new_dlldump = vol.dlldump(dump_dir=resdir, pid=pid, base=base)
				mem_analysis["diffs"]["injected_dll"] = {"star" : "yes", "new": new_dlldump["data"], "deleted": [], "desc" : "Finds the injected dll and dumps it"}
		    	    elif trigger == "NtSetContextThread -> NtResumeThread":
				pid = str(args["ProcessId"])
				tid = int(args["ThreadId"])
				mem_analysis["diffs"]["injected_thread"] = {"new":[],"deleted":[],"star":"yes", "desc":"Shows the injected thread"}
				threads = vol.threads(pid=pid)["data"]
				for t in threads:
					if t["TID"] == tid:
						mem_analysis["diffs"]["injected_thread"]["new"].append(t)
		    	    elif trigger == "WriteProcessMemory -> CreateRemoteThread":
				
				start = int(args["StartRoutine"], 16)
				dir1 = tempfile.mkdtemp()
				dir2 = tempfile.mkdtemp()
				pid = str(args["ProcessId"])
                                tid = int(args["ThreadId"])
				dis = None
                                try:
                                        dis = disasm_from_memory(self.memfile, int(pid), start, 100)
                                except:
                                        pass

                                mem_analysis["diffs"]["injected_thread"] = {"new":[],"deleted":[],"star":"yes", "desc":"Shows the injected thread", "disassembly" : dis}
                                threads = vol.threads(pid=pid)["data"]
                                for t in threads:
                                        if t["TID"] == tid:
                                                mem_analysis["diffs"]["injected_thread"]["new"].append(t)

				shutil.rmtree(dir1)
				shutil.rmtree(dir2)
				#if static:
				#	mem_analysis["triggered"]["static_analysis"] = static

		return mem_analysis, new_results


