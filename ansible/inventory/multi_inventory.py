#!/usr/bin/env python2
'''
    Fetch and combine multiple inventory account settings into a single
    json hash.
'''
# vim: expandtab:tabstop=4:shiftwidth=4

from time import time
import argparse
import yaml
import os
import subprocess
import json
import errno
import fcntl
import tempfile
import copy
from string import Template
import shutil

CONFIG_FILE_NAME = 'multi_inventory.yaml'
DEFAULT_CACHE_PATH = os.path.expanduser('~/.ansible/tmp/multi_inventory.cache')

class MultiInventoryException(Exception):
    '''Exceptions for MultiInventory class'''
    pass

# pylint: disable=too-many-public-methods
# After a refactor of too-many-branches and placing those branches into
# their own corresponding function, we have passed the allowed amount of functions(20).
class MultiInventory(object):
    '''
       MultiInventory class:
            Opens a yaml config file and reads aws credentials.
            Stores a json hash of resources in result.
    '''

    def __init__(self, args=None):
        # Allow args to be passed when called as a library
        if not args:
            self.args = {}
        else:
            self.args = args

        self.cache_path = DEFAULT_CACHE_PATH
        self.config = None
        self.all_inventory_results = {}
        self.result = {}
        self.file_path = os.path.join(os.path.dirname(os.path.realpath(__file__)))

        same_dir_config_file = os.path.join(self.file_path, CONFIG_FILE_NAME)
        etc_dir_config_file = os.path.join(os.path.sep, 'etc', 'ansible', CONFIG_FILE_NAME)

        # Prefer a file in the same directory, fall back to a file in etc
        if os.path.isfile(same_dir_config_file):
            self.config_file = same_dir_config_file
        elif os.path.isfile(etc_dir_config_file):
            self.config_file = etc_dir_config_file
        else:
            self.config_file = None # expect env vars

        # load yaml
        if self.config_file and os.path.isfile(self.config_file):
            self.config = self.load_yaml_config()
        elif os.environ.has_key("AWS_ACCESS_KEY_ID") and \
             os.environ.has_key("AWS_SECRET_ACCESS_KEY"):
            # Build a default config
            self.config = {}
            # pylint: disable=line-too-long
            self.config['accounts'] = {'default': {'cache_location': DEFAULT_CACHE_PATH,
                                                   'provider': 'aws/hosts/ec2.py',
                                                   'env_vars': {'AWS_ACCESS_KEY_ID':     os.environ["AWS_ACCESS_KEY_ID"],
                                                                'AWS_SECRET_ACCESS_KEY': os.environ["AWS_SECRET_ACCESS_KEY"],
                                                               }
                                                  }
                                      }

            self.config['cache_max_age'] = 300
        else:
            raise RuntimeError("Could not find valid ec2 credentials in the environment.")

        if self.config.has_key('cache_location'):
            self.cache_path = self.config['cache_location']

    def run(self):
        '''This method checks to see if the local
           cache is valid for the inventory.

           if the cache is valid; return cache
           else the credentials are loaded from multi_inventory.yaml or from the env
           and we attempt to get the inventory from the provider specified.
        '''

        if self.args.get('refresh_cache', None):
            self.get_inventory()
            self.write_to_cache()
        # if its a host query, fetch and do not cache
        elif self.args.get('host', None):
            self.get_inventory()
        elif not self.is_cache_valid():
            # go fetch the inventories and cache them if cache is expired
            self.get_inventory()
            self.write_to_cache()
        else:
            # get data from disk
            self.get_inventory_from_cache()

    def load_yaml_config(self, conf_file=None):
        """Load a yaml config file with credentials to query the
        respective cloud for inventory.
        """
        config = None

        if not conf_file:
            conf_file = self.config_file

        with open(conf_file) as conf:
            config = yaml.safe_load(conf)

        return config

    def get_provider_tags(self, provider, env=None):
        """Call <provider> and query all of the tags that are usuable
        by ansible.  If environment is empty use the default env.
        """
        if not env:
            env = os.environ

        # Allow relatively path'd providers in config file
        if os.path.isfile(os.path.join(self.file_path, provider)):
            provider = os.path.join(self.file_path, provider)

        # check to see if provider exists
        if not os.path.isfile(provider) or not os.access(provider, os.X_OK):
            raise RuntimeError("Problem with the provider.  Please check path " \
                        "and that it is executable. (%s)" % provider)

        cmds = [provider]
        if self.args.get('host', None):
            cmds.append("--host")
            cmds.append(self.args.get('host', None))
        else:
            cmds.append('--list')

        if 'aws' in provider.lower():
            cmds.append('--refresh-cache')

        return subprocess.Popen(cmds, stderr=subprocess.PIPE, \
                                stdout=subprocess.PIPE, env=env)

    @staticmethod
    def generate_config(provider_files):
        """Generate the provider_files in a temporary directory.
        """
        prefix = 'multi_inventory.'
        tmp_dir_path = tempfile.mkdtemp(prefix=prefix)
        for provider_file in provider_files:
            filedes = open(os.path.join(tmp_dir_path, provider_file['name']), 'w+')
            content = Template(provider_file['contents']).substitute(tmpdir=tmp_dir_path)
            filedes.write(content)
            filedes.close()

        return tmp_dir_path

    def run_provider(self):
        '''Setup the provider call with proper variables
           and call self.get_provider_tags.
        '''
        try:
            all_results = []
            tmp_dir_paths = []
            processes = {}
            for acc_name, account in self.config['accounts'].items():
                tmp_dir = None
                if account.has_key('provider_files'):
                    tmp_dir = MultiInventory.generate_config(account['provider_files'])
                    tmp_dir_paths.append(tmp_dir)

                # Update env vars after creating provider_config_files
                # so that we can grab the tmp_dir if it exists
                env = account.get('env_vars', {})
                if env and tmp_dir:
                    for key, value in env.items():
                        env[key] = Template(value).substitute(tmpdir=tmp_dir)

                provider = account['provider']
                processes[acc_name] = self.get_provider_tags(provider, env)

            # for each process collect stdout when its available
            for name, process in processes.items():
                out, err = process.communicate()
                all_results.append({
                    "name": name,
                    "out": out.strip(),
                    "err": err.strip(),
                    "code": process.returncode
                })

        finally:
            # Clean up the mkdtemp dirs
            for tmp_dir in tmp_dir_paths:
                shutil.rmtree(tmp_dir)

        return all_results

    def get_inventory(self):
        """Create the subprocess to fetch tags from a provider.
        Host query:
        Query to return a specific host.  If > 1 queries have
        results then fail.

        List query:
        Query all of the different accounts for their tags.  Once completed
        store all of their results into one merged updated hash.
        """
        provider_results = self.run_provider()

        # process --host results
        # For any 0 result, return it
        if self.args.get('host', None):
            count = 0
            for results in provider_results:
                if results['code'] == 0 and results['err'] == '' and results['out'] != '{}':
                    self.result = json.loads(results['out'])
                    count += 1
                if count > 1:
                    raise RuntimeError("Found > 1 results for --host %s. \
                                       This is an invalid state." % self.args.get('host', None))
        # process --list results
        else:
            # For any non-zero, raise an error on it
            for result in provider_results:
                if result['code'] != 0:
                    err_msg = ['\nProblem fetching account: {name}',
                               'Error Code: {code}',
                               'StdErr: {err}',
                               'Stdout: {out}',
                              ]
                    raise RuntimeError('\n'.join(err_msg).format(**result))
                else:
                    self.all_inventory_results[result['name']] = json.loads(result['out'])

            # Check if user wants extra vars in yaml by
            # having hostvars and all_group defined
            for acc_name, acc_config in self.config['accounts'].items():
                self.apply_account_config(acc_name, acc_config)

            # Build results by merging all dictionaries
            values = self.all_inventory_results.values()
            values.insert(0, self.result)
            for result in  values:
                MultiInventory.merge_destructively(self.result, result)

    def add_entry(self, data, keys, item):
        ''' Add an item to a dictionary with key notation a.b.c
            d = {'a': {'b': 'c'}}}
            keys = a.b
            item = c
        '''
        if "." in keys:
            key, rest = keys.split(".", 1)
            if key not in data:
                data[key] = {}
            self.add_entry(data[key], rest, item)
        else:
            data[keys] = item

    def get_entry(self, data, keys):
        ''' Get an item from a dictionary with key notation a.b.c
            d = {'a': {'b': 'c'}}}
            keys = a.b
            return c
        '''
        if keys and "." in keys:
            key, rest = keys.split(".", 1)
            if data.has_key(key):
                return self.get_entry(data[key], rest)

            return None
        else:
            return data.get(keys, None)

    def apply_cluster_vars(self, inventory, cluster_vars):
        ''' Apply the account config cluster vars '''
        # cluster vars go here
        # do nothing for accounts that do not have cluster vars
        if not cluster_vars:
            return

        cluster_tag = cluster_vars['cluster_tag']
        synthetic_hosts = cluster_vars.get('synthetic_hosts', False)

        for cluster_name, cluster in cluster_vars['clusters'].items():
            for host in inventory['_meta']['hostvars'].values():
                clusterid = self.get_entry(host, cluster_tag)

                if clusterid == cluster_name:
                    self.add_entry(host, 'oo_clusterid', clusterid)

                    for new_var, value in cluster.items():
                        self.add_entry(host, new_var, value)

            # Apply synthetic host groups for boot strapping purposes
            if synthetic_hosts:
                synth_host = 'synthetic_%s' % cluster_name

                inventory['_meta']['hostvars'][synth_host] = {'oo_clusterid': cluster_name}

                for new_var, value in cluster.items():
                    inventory['_meta']['hostvars'][synth_host][new_var] = value

                if not inventory.has_key('synthetic_hosts'):
                    inventory['synthetic_hosts'] = []

                inventory['synthetic_hosts'].append(synth_host)

    def apply_extra_vars(self, inventory, extra_vars):
        ''' Apply the account config extra vars '''
        # Extra vars go here
        for new_var, value in extra_vars.items():
            for data in inventory.values():
                self.add_entry(data, new_var, value)

    def apply_clone_vars(self, inventory, clone_vars):
        ''' Apply the account config clone vars '''
        # Clone vars go here
        for to_name, from_name in clone_vars.items():
            for data in inventory.values():
                self.add_entry(data, to_name, self.get_entry(data, from_name))

    def apply_extra_groups(self, inventory, extra_groups):
        ''' Apply the account config for extra groups '''
        _ = self # Here for pylint.  wanted an instance method instead of static
        for new_var, value in extra_groups.items():
            for name, _ in inventory['_meta']['hostvars'].items():
                if 'synthetic_' in name:
                    continue

                inventory["%s_%s" % (new_var, value)] = copy.copy(inventory['all_hosts'])

    def apply_clone_groups(self, inventory, clone_groups):
        ''' Apply the account config for clone groups '''
        for to_name, from_name in clone_groups.items():
            for name, data in inventory['_meta']['hostvars'].items():
                if 'synthetic_' in name:
                    continue

                key = '%s_%s' % (to_name, self.get_entry(data, from_name))
                if not inventory.has_key(key):
                    inventory[key] = []
                inventory[key].append(name)

    def apply_group_selectors(self, inventory, group_selectors):
        ''' Apply the account config for group selectors '''
        _ = self # Here for pylint.  wanted an instance method instead of static
        # There could be multiple clusters per account.  We need to process these selectors
        # based upon the oo_clusterid_ variable.
        clusterids = [group for group in inventory if "oo_clusterid_" in group]

        for clusterid in clusterids:
            for selector in group_selectors:
                if inventory.has_key(selector['from_group']):
                    hosts = list(set(inventory[clusterid]) & set(inventory[selector['from_group']]))
                    hosts.sort()

                    # Multiple clusters in an account
                    if inventory.has_key(selector['name']):
                        inventory[selector['name']].extend(hosts[0:selector['count']])
                    else:
                        inventory[selector['name']] = hosts[0:selector['count']]

                    for host in hosts:
                        if host in inventory[selector['name']]:
                            inventory['_meta']['hostvars'][host][selector['name']] = True
                        else:
                            inventory['_meta']['hostvars'][host][selector['name']] = False

    def apply_account_config(self, acc_name, acc_config):
        ''' Apply account config settings '''
        results = self.all_inventory_results[acc_name]

        results['all_hosts'] = results['_meta']['hostvars'].keys()

        self.apply_cluster_vars(results, acc_config.get('cluster_vars', {}))

        self.apply_extra_vars(results['_meta']['hostvars'], acc_config.get('extra_vars', {}))

        self.apply_clone_vars(results['_meta']['hostvars'], acc_config.get('clone_vars', {}))

        self.apply_extra_groups(results, acc_config.get('extra_groups', {}))

        self.apply_clone_groups(results, acc_config.get('clone_groups', {}))

        self.apply_group_selectors(results, acc_config.get('group_selectors', {}))

        # store the results back into all_inventory_results
        self.all_inventory_results[acc_name] = results

    @staticmethod
    def merge_destructively(input_a, input_b):
        "merges b into input_a"
        for key in input_b:
            if key in input_a:
                if isinstance(input_a[key], dict) and isinstance(input_b[key], dict):
                    MultiInventory.merge_destructively(input_a[key], input_b[key])
                elif input_a[key] == input_b[key]:
                    pass # same leaf value
                # both lists so add each element in b to a if it does ! exist
                elif isinstance(input_a[key], list) and isinstance(input_b[key], list):
                    for result in input_b[key]:
                        if result not in input_a[key]:
                            input_a[key].append(result)
                # a is a list and not b
                elif isinstance(input_a[key], list):
                    if input_b[key] not in input_a[key]:
                        input_a[key].append(input_b[key])
                elif isinstance(input_b[key], list):
                    input_a[key] = [input_a[key]] + [k for k in input_b[key] if k != input_a[key]]
                else:
                    input_a[key] = [input_a[key], input_b[key]]
            else:
                input_a[key] = input_b[key]
        return input_a

    def is_cache_valid(self):
        ''' Determines if the cache files have expired, or if it is still valid '''

        if os.path.isfile(self.cache_path):
            mod_time = os.path.getmtime(self.cache_path)
            current_time = time()
            if (mod_time + self.config['cache_max_age']) > current_time:
                return True

        return False

    def parse_cli_args(self):
        ''' Command line argument processing '''

        parser = argparse.ArgumentParser(
            description='Produce an Ansible Inventory file based on a provider')
        parser.add_argument('--refresh-cache', action='store_true', default=False,
                            help='Fetch cached only instances (default: False)')
        parser.add_argument('--list', action='store_true', default=True,
                            help='List instances (default: True)')
        parser.add_argument('--host', action='store', default=False,
                            help='Get all the variables about a specific instance')
        self.args = parser.parse_args().__dict__

    def write_to_cache(self):
        ''' Writes data in JSON format to a file '''

        # if it does not exist, try and create it.
        if not os.path.isfile(self.cache_path):
            path = os.path.dirname(self.cache_path)
            try:
                os.makedirs(path)
            except OSError as exc:
                if exc.errno != errno.EEXIST or not os.path.isdir(path):
                    raise

        json_data = MultiInventory.json_format_dict(self.result, True)
        with open(self.cache_path, 'w') as cache:
            try:
                fcntl.flock(cache, fcntl.LOCK_EX)
                cache.write(json_data)
            finally:
                fcntl.flock(cache, fcntl.LOCK_UN)

    def get_inventory_from_cache(self):
        ''' Reads the inventory from the cache file and returns it as a JSON
        object '''

        if not os.path.isfile(self.cache_path):
            return None

        with open(self.cache_path, 'r') as cache:
            self.result = json.loads(cache.read())

        return True

    @classmethod
    def json_format_dict(cls, data, pretty=False):
        ''' Converts a dict to a JSON object and dumps it as a formatted
        string '''

        if pretty:
            return json.dumps(data, sort_keys=True, indent=2)
        else:
            return json.dumps(data)

    def result_str(self):
        '''Return cache string stored in self.result'''
        return self.json_format_dict(self.result, True)


if __name__ == "__main__":
    MI2 = MultiInventory()
    MI2.parse_cli_args()
    MI2.run()
    print MI2.result_str()
