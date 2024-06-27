# linux_explorer.py

import hashlib
import os
import subprocess
import time

import IndicatorTypes
import psutil
import requests
from OTXv2 import OTXv2
from flask import Flask
from flask import abort
from flask import jsonify
from flask import redirect
from flask import render_template
from flask import request
from flask import send_file
from flask import send_from_directory
from hulnasoft_analyze_sdk import api
from hulnasoft_analyze_sdk import errors
from hulnasoft_analyze_sdk.analysis import Analysis
from werkzeug.utils import secure_filename

import config
import tools

app = Flask(__name__)

toolbox = dict({'yara': tools.YARA(),
                'chkrootkit': tools.Chkrootkit(),
                'find': tools.Find()})


@app.route('/')
def index():
    return redirect('/processes')


@app.route('/processes')
def processes():
    return render_template('processes.html'), 200


@app.route('/processes/list')
def processes_list():
    ''' process list, kthreads filtered out. '''

    return jsonify({'data': list(filter(lambda pinfo: pinfo['pid'] != 2 and pinfo['ppid'] != 2,
                                        map(lambda pinfo: pinfo.as_dict(), psutil.process_iter())))}), 200


@app.route('/processes/<int:pid>/memory_map')
def process_memory_map(pid):
    return jsonify(
        {'data': list(map(lambda pmmap_ext: pmmap_ext._asdict(), psutil.Process(pid).memory_maps(grouped=False)))}), 200


@app.route('/processes/<int:pid>/connections')
def process_connections(pid):
    return jsonify({'data': list(map(lambda pconn: pconn._asdict(), psutil.Process(pid).connections()))}), 200


@app.route('/processes/<int:pid>/core_file')
def process_gcore(pid):
    def dump(folder, pid):

        timestamp = str(int(time.time()))

        if not os.system('gcore -o %s %d' % (os.path.join(folder, timestamp), pid)):  # check for free space before
            for filename in os.listdir(folder):
                if filename.startswith(timestamp):
                    return filename

        return None

    CORE_FILES = 'static/core_files'

    if not os.path.exists(CORE_FILES):
        os.mkdir(CORE_FILES)

    core_file = dump(CORE_FILES, pid)

    return send_from_directory(directory='static/core_files', filename=core_file)  # add error check/log


@app.route('/mem/<int:pid>/strings')
def mem_strings(pid):
    STRINGS = 'static/strings'
    if not os.path.exists(STRINGS):
        os.mkdir(STRINGS)

    start = request.args.get('start', '')
    end = request.args.get('end', '')
    filename = "%s.%s_%s" % (pid, start, end)
    dump_file = os.path.join(STRINGS, filename + ".dmp")
    strings_file = os.path.join(STRINGS, filename + ".strings")

    def dump(pid, start, end, output_file):
        os.system('gdb --batch --pid %d -ex \"dump memory %s 0x%s 0x%s\"' % (pid, output_file, start, end))

    def strings(path, output_file):
        os.system('strings %s > %s' % (path, output_file))

    dump(pid, start, end, dump_file)
    strings(dump_file, strings_file)

    os.remove(dump_file)

    return send_from_directory(directory=STRINGS, filename=filename + '.strings', as_attachment=True)


@app.route('/fs/hash')
def fs_hash():
    md5 = hashlib.md5(open(request.args.get('path', ''), 'rb').read()).hexdigest()
    sha256 = hashlib.sha256(open(request.args.get('path', ''), 'rb').read()).hexdigest()

    return jsonify({'md5': md5, 'sha256': sha256}), 200


@app.route('/fs/download')
def fs_download():
    return send_file(request.args.get('path', ''), as_attachment=True)


@app.route('/vt/report/<string:hash>')
def vt_report(hash):
    ''' fetch VirusTotal report. Simple proxy to overcome same origin policy. '''

    if not len(config.VT_APIKEY):
        return jsonify({"error": "NO API KEY"}), 200

    response = requests.get('https://www.virustotal.com/vtapi/v2/file/report', params={'apikey': config.VT_APIKEY,
                                                                                       'resource': hash},
                            headers={'Accept-Encoding': 'gzip, deflate',
                                     'User-Agent': 'gzip,  Linux Expl0rer'})

    return jsonify(response.json() if response.status_code == 200 else response.text), response.status_code


@app.route('/vt/upload')
def vt_upload():
    if not len(config.VT_APIKEY):
        return jsonify({"error": "NO API KEY"}), 200

    path = request.args.get('path', '')

    if not os.path.isfile(path):
        return jsonify({"error": "%s is not a valid file or the system could not access it" % path}), 200

    files = {'file': (os.path.basename(path), open(path, 'rb'))}

    response = requests.post('https://www.virustotal.com/vtapi/v2/file/scan', params={'apikey': config.VT_APIKEY},
                             files=files,
                             headers={'Accept-Encoding': 'gzip, deflate',
                                      'User-Agent': 'gzip,  Linux Expl0rer'})

    return jsonify(response.json() if response.status_code == 200 else response.text), response.status_code


@app.route('/khulnasoft/upload')
def khulnasoft_upload():
    if not len(config.KHULNASOFT_APIKEY):
        return jsonify({"error": "NO API KEY"}), 200

    path = request.args.get('path', '')

    if not os.path.isfile(path):
        return jsonify({"error": "%s is not a valid file or the system could not access it" % path}), 200

    try:
        api.set_global_api(config.KHULNASOFT_APIKEY)
        analysis = Analysis(file_path=path,
                            dynamic_unpacking=None,
                            static_unpacking=None)

        analysis.send(True)
    except errors.KhulnasoftError as e:
        return jsonify({"error": "Error occurred: " + e.args[0]}), 200

    return jsonify(analysis.result()), 200


@app.route('/otx/<string:type>/<string:indicator>')
def otx_report(type, indicator):
    ''' AlienVault Open Threat Exchange interface.
        Supported indicator types: "IPv4", "IPv6", "domain", "hostname", "URL", "FileHash-MD5", "FileHash-SHA1", "FileHash-SHA256", "CVE". '''

    if type not in IndicatorTypes.to_name_list(IndicatorTypes.supported_api_types):
        raise Exception("Indicator type %s not supported!" % type)

    def find_by_type(type):
        for indicator in IndicatorTypes.supported_api_types:
            if indicator.name == type:
                return indicator
        raise Exception("Indicator type %s not supported!" % type)

    if not len(config.OTX_APIKEY):
        return jsonify({"error": "NO API KEY"}), 200

    otx = OTXv2(config.OTX_APIKEY, server='https://otx.alienvault.com/')

    return jsonify(otx.get_indicator_details_full(find_by_type(type), indicator)), 200


@app.route('/malshare/<string:hash_>')
def malshare_report(hash_):
    ''' MalShare repository.
        Supported indicator types: "FileHash-MD5", "FileHash-SHA1", "FileHash-SHA256", '''

    if not len(config.MALSHARE_APIKEY):
        return jsonify({"error": "NO API KEY"}), 200

    response = requests.get(
        'https://malshare.com/api.php?api_key=%s&action=details&hash=%s' % (config.MALSHARE_APIKEY, hash_),
        headers={'Accept-Encoding': 'gzip, deflate', 'User-Agent': 'gzip,  Linux Expl0rer'})

    return jsonify(response.json() if not response.text.startswith(
        'Sample not found by hash (') else response.text), response.status_code


@app.route('/logs/<string:file>')
def logs(file):
    if file == "system":
        log_path = "/var/log/syslog" if config.IS_UBUNTU else "/var/log/messages"

    elif file == "authentication":
        log_path = "/var/log/auth.log" if config.IS_UBUNTU else "/var/log/secure"

    elif file == "firewall":
        log_path = "/var/log/ufw.log" if config.IS_UBUNTU else "/var/log/firewalld"

    elif file == "bash":
        log_path = os.path.expanduser('~/.bash_history')

    else:
        abort(404)

    if os.path.isfile(log_path):
        with open(log_path, 'r') as fh:
            log_data = fh.read()

    else:
        log_data = "- not found -"

    return render_template('logs_view.html', text=log_data), 200


@app.route('/netstat')
def netstat():
    return render_template('netstat.html'), 200


@app.route('/netstat/raw')
def netstat_raw():
    return jsonify({'data': list(map(lambda sconn: sconn._asdict(), psutil.net_connections()))}), 200


@app.route('/sh')
def sh():
    return render_template('sh.html'), 200


@app.route('/sh/exec')
def shell():
    return subprocess.Popen(request.args.get('cmdline', ''), shell=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT).communicate()[0]


@app.route('/yara')
def yara():
    return render_template('yara.html',
                           ruleset_files=list(map(lambda x: x.split('.yar')[0], os.listdir('yara_rules')))), 200


@app.route('/yara/upload', methods=['GET', 'POST'])
def yara_upload():
    if request.method == 'POST':
        request.files['file'].save(os.path.join('yara_rules', secure_filename(request.files['file'].filename)))

    return redirect("/yara", code=302)


@app.route('/tools/<string:tool>/run')
def tools_run(tool):
    if tool == 'yara':
        if request.args.get('pid', None):
            toolbox['yara'].set_cmdline('yara_rules/' + request.args.get('rules_file', '') + '.yar',
                                        request.args.get('pid', ''))
        else:
            toolbox['yara'].set_cmdline('yara_rules/' + request.args.get('rules_file', '') + '.yar',
                                        request.args.get('dir', ''),
                                        request.args.get('recursive', 'true') == 'true')

        toolbox['yara'].run()

    elif tool == 'chkrootkit':
        toolbox['chkrootkit'].set_cmdline()
        toolbox['chkrootkit'].run()

    elif tool == 'find':
        toolbox['find'].set_cmdline(request.args.get('dir', ''),
                                    request.args.get('name', ''))

        toolbox['find'].run()

    else:
        abort(404)

    return "", 200


@app.route('/tools/<string:tool>/status')
def tools_status(tool):
    if tool not in toolbox:
        abort(404)

    return toolbox[tool].status(), 200


@app.route('/tools/<string:tool>/results')
def tools_results(tool):
    if tool not in toolbox:
        abort(404)

    return toolbox[tool].results(), 200


@app.route('/tools/<string:tool>/stop')
def tools_stop(tool):
    if tool not in toolbox:
        abort(404)

    toolbox[tool].stop()

    return "", 200


@app.route('/users')
def users():
    return render_template('users.html'), 200


@app.route('/users/list')
def users_list():
    with open('/etc/passwd', 'r') as fh:
        users = list(map(lambda line: line.split(':'), fh.readlines()))

    return jsonify({'data': users}), 200


@app.route('/files')
def files():
    return render_template('files.html'), 200


@app.route('/chkrootkit')
def chkrootkit():
    return render_template('chkrootkit.html'), 200


if __name__ == '__main__':
    app.run('127.0.0.1', 8080, debug=True)
