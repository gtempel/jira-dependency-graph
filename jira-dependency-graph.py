#!/usr/bin/env python

from __future__ import print_function

import argparse
import getpass
import sys
import textwrap
import requests
from functools import reduce

GOOGLE_CHART_URL = 'https://chart.apis.google.com/chart'
MAX_SUMMARY_LENGTH = 30


def log(*args):
    print(*args, file=sys.stderr)

class JiraGraph(object):
    """ This object holds the graph data for the nodes we create while we
        traverse the Jira cases and links. It's providing a wrapper around the specific
        method of storage so we can abstract it.
    """
    __graph_data = []
    __seen = []

    def add_issue_node(self, node):
        self.__graph_data.append(node)
    
    def add_link_node(self, node):
        self.__graph_data.append(node)

    def mark_as_seen(self, issue_key):
        self.__seen.append(issue_key)
    
    def has_seen(self, issue_key):
        return issue_key in self.__seen
    
    def generate_digraph(self, default_node_shape):
        """
            This method takes the graph information and converts it to dot (graphviz) notation,
            returning the dot description as a string to the caller.
        """
        graph_defaults = 'graph [rankdir=LR];' # splines=ortho
        node_defaults = 'node [fontname=Helvetica, shape=' + default_node_shape +'];'
        digraph = 'digraph{' + node_defaults + graph_defaults + '%s}' % ';'.join(self.__graph_data)
        return digraph

class JiraGraphRenderer(object):
    """ Refactored rendering information from the JiraGraph to here. This class'
        responsibilities are rendering the graph to a dot (graphviz) file as well
        as (potentially) a png via a web service call (not working at the moment)
    """

    def generate_dotfile(self, graph, default_node_shape, filename='graph_data.dot'):
        """
            Given the graph object, ask it to be rendered to a dot file
            then write that file to storage using the given filename.
        """
        digraph = graph.generate_digraph(default_node_shape)
        with open(filename, "w") as dotfile:
            dotfile.write(digraph)
            dotfile.close()
        return digraph

    def render(self, graph, default_node_shape, filename='issue_graph.png'):
        """ Given a formatted blob of graphviz chart data[1], make the actual request to Google
            and store the resulting image to disk.

            [1]: http://code.google.com/apis/chart/docs/gallery/graphviz.html
        """
        digraph = graph.generate_digraph(default_node_shape)
        print('sending: ', GOOGLE_CHART_URL, {'cht':'gv', 'chl': digraph})

        response = requests.post(GOOGLE_CHART_URL, data = {'cht':'gv', 'chl': digraph})

        with open(filename, 'w+b') as image:
            print('Writing to ' + filename)
            binary_format = bytearray(response.content)
            image.write(binary_format)
            image.close()
        return filename

    # do we really need this?
    # def filter_duplicates(self, lst):
    #     # Enumerate the list to restore order lately; reduce the sorted list; restore order
    #     def append_unique(acc, item):
    #         return acc if acc[-1][1] == item[1] else acc.append(item) or acc
    #     srt_enum = sorted(enumerate(lst), key=lambda i_val: i_val[1])
    #     return [item[1] for item in sorted(reduce(append_unique, srt_enum, [srt_enum[0]]))]

class JiraSearch(object):
    """ This factory will create the actual method used to fetch issues from JIRA. This is really just a closure that
        saves us having to pass a bunch of parameters all over the place all the time. """

    __base_url = None
    __customfields = None

    def __init__(self, url, auth, no_verify_ssl, extra_issue_fields = []):
        self.__base_url = url
        self.url = url + '/rest/api/latest'
        self.auth = auth
        self.no_verify_ssl = no_verify_ssl

        # Every installation of Jira is different, and things like 'Epic Link' are
        # assigned to different custom fields. Additionally, the caller (and human behind it)
        # may be asking for those fields whenever we deal with an issue. Jira doesn't use
        # the "name" of the custom fields in the JQL, it wants the key (the customfield_XXX),
        # so we need to hold onto the association and be able to translate betweeen the two.
        self.__customfields = self.get_customfields()
        fieldnames = ['key', 'summary', 'status', 'description', 'issuetype', 'issuelinks', 'subtasks']
        
        # if the caller asked for extra fields (such as 'Epic Link', etc)
        self.__fields_to_map = {}
        for extra_issue_field_name in extra_issue_fields:
            customfield_name = self.__customfields.get(extra_issue_field_name, None)
            if customfield_name:
                fieldnames.append(customfield_name)
                self.__fields_to_map[customfield_name] = extra_issue_field_name
        self.fields = ','.join(fieldnames)

    def get_customfields(self):
        """
            Uses the Jira API to fetch a list of all of the custom field definitions, where
            each field is a json object. Of note in the json objects are the keys 'key', which
            resolves to things like 'customfield_21521' and 'name' which is the name of the
            custom field like 'Developer Checklist reviewed'.

            This method will fetch that list and convert it into a map of customfield names and
            customfield keys, creating a mapping of name:customfield.
        """ 
        if not self.__customfields:
            response = self.get('/field')
            response.raise_for_status()
            field_list = response.json()
            self.__customfields = {item['name']:item['key'] for item in field_list if item['key'].startswith('customfield_')}
        return self.__customfields

    def get(self, uri, params={}):
        headers = {'Content-Type' : 'application/json'}
        url = self.url + uri

        if isinstance(self.auth, str):
            return requests.get(url, params=params, cookies={'JSESSIONID': self.auth}, headers=headers, verify=self.no_verify_ssl)
        else:
            return requests.get(url, params=params, auth=self.auth, headers=headers, verify=(not self.no_verify_ssl))

    def get_labels(self, labels_to_find):
        labels = []
        if labels_to_find:
            labels_to_find = set(labels_to_find)
            start_at = 0
            is_last = False
            while not is_last:
                response = self.get('/label?startAt={startAt}'.format(startAt=start_at))
                response.raise_for_status()
                payload = response.json()
                # use 'all' below to AND the labels_to_find (has to match each of the find terms)
                # use 'any' below to OR the labels_to_find (has to match at least one find term)
                new_labels = [label for label in payload['values'] if all(sub in label for sub in labels_to_find)]
                if new_labels:
                    labels = labels + new_labels
                start_at = start_at + len(payload['values'])
                is_last = payload['isLast']

        return labels

    def get_mapped_issue_fields(self, issue_key):
        """
            Map things in fields like 'customfield_10234' to 'Epic Link' by using
            the fieldmap which contains the oldkey:newkeyMapping. The fields will
            then contain new items newkeyMapping:originalValue pairs and the
            oldkey:originalValue pairs will be removed.
        """
        issue = self.get_issue(issue_key)
        fields = issue['fields']

        for k, v in self.__fields_to_map.items():
            fields[v] = fields[k]

        for k in self.__fields_to_map.keys():
            fields.pop(k, None)
        
        return fields


    def get_issue(self, key):
        """ Given an issue key (i.e. JRA-9) return the JSON representation of it. This is the only place where we deal
            with JIRA's REST API. """
        log('Fetching ' + key)
        # we need to expand subtasks and links since that's what we care about here.
        response = self.get('/issue/%s' % key, params={'fields': self.fields})
        response.raise_for_status()
        return response.json()

    def query(self, query):
        log('Querying ' + query)
        response = self.get('/search', params={'jql': query, 'fields': self.fields})
        content = response.json()
        return content['issues']

    def get_issue_uri(self, issue_key):
        return self.__base_url + '/browse/' + issue_key

class JiraOptions(object):
    """
        This class is used to embody the options or parameters, as specified by
        defaults or the user. We're employing a trick here to allow for
        named parameters, one for each possible option, and don't want to have to
        keep adjusting the options object (or parameters to other class' methods)
        every time we create a new option. Thus, we're allowing a dictionary as
        the parameter, and we're merging that dictionary's key/value pairs into
        this object so they can be directly referenced as instance variables of
        the object.
    """
    def __init__(self, kwargs):
        self.__dict__.update(kwargs)


def build_graph_data(graph,
                     start_issue_key, 
                     jira, 
                     jira_options):
    """ Given a starting image key and the issue-fetching function build up the GraphViz data representing relationships
        between issues. This will consider both subtasks and issue links, among other things, as per the jira_options.
    """

    def get_key(issue):
        return issue['key']

    def get_status_color(status_field):
        default_color = 'white'
        colors = {
            'IN PROGRESS': 'yellow',
            'DONE': 'green',
            'BLOCKED': 'red',
            'BLOCKS' : 'red'
        }
        status = status_field['statusCategory']['name'].upper()
        color = colors.get(status, default_color)
        return color

    def get_extra_decorations_for_link_type(link_type):
        extra = ',color="red", penwidth=4.0' if link_type == "blocks" else ""
        return extra

    def get_extra_decorations_for_status(status_field):
        status = status_field['name'].upper()
        extra = ',color="red", penwidth=4.0' if "BLOCK" in status else ''
        return extra

    def get_issue_type(fields):
        issue_type = fields['issuetype']['name']
        return issue_type

    def get_node_shape(issue_key, fields, default_shape='rect'):
        shapes = {
            "Epic": "oval", #"diamond",
            "Story": default_shape,
            "Spike": default_shape,
            "subtask": "text", #"oval",
            "Task": "MCircle",
            "Certified": "octagon"
        }

        issue_type = get_issue_type(fields)
        shape = shapes.get(issue_type, default_shape)
        return shape

    def create_node_name(issue_key, fields):
        no_issue_type_prefixes = [
            'Story',
            'Certified',
            'Task',
            'ACL (Access Control Language)'
        ]
        
        issue_type = get_issue_type(fields)
        if issue_type in no_issue_type_prefixes:
            return issue_key
        return '{} {}'.format(issue_type, issue_key)

    def create_node_text(issue_key, fields, islink=True):
        default_shape = 'rect'
        issue_shape = get_node_shape(issue_key, fields, default_shape)
        issue_name = create_node_name(issue_key, fields)

        summary = fields['summary']
        status = fields['status']

        if jira_options.word_wrap == True:
            if len(summary) > MAX_SUMMARY_LENGTH:
                # split the summary into multiple lines adding a \n to each line
                summary = textwrap.fill(summary, MAX_SUMMARY_LENGTH)
        else:
            # truncate long labels with "...", but only if the three dots are replacing more than two characters
            # -- otherwise the truncated label would be taking more space than the original.
            if len(summary) > MAX_SUMMARY_LENGTH + 2:
                summary = summary[:MAX_SUMMARY_LENGTH] + '...'
        summary = summary.replace('"', '\\"')

        if islink:
            return '"{}\\n({})"'.format(issue_name, summary)
        
        extras = get_extra_decorations_for_status(status)
        return '"{}\\n({})" [shape="{}", href="{}", fillcolor="{}", style=filled {}]'.format(issue_name, 
                                                                                            summary, 
                                                                                            issue_shape, 
                                                                                            jira.get_issue_uri(issue_key), 
                                                                                            get_status_color(status),
                                                                                            extras)

    def should_ignore_issue(issue):
        issue_key = get_key(issue)
        issue_type = get_issue_type(issue['fields'])
        if (jira_options.issue_excludes and (issue_key in jira_options.issue_excludes)) or (jira_options.ignore_types and (issue_type in jira_options.ignore_types)):
            if jira_options.verbose:
                log('Issue ' + issue_key + ' - should be ignored')
            return True
        return False

    def process_link(fields, issue_key, link):
        if 'outwardIssue' in link:
            direction = 'outward'
        elif 'inwardIssue' in link:
            direction = 'inward'
        else:
            return

        if direction not in jira_options.directions:
            return


        link_issue_direction = direction + 'Issue'
        linked_issue = link[link_issue_direction]
        linked_issue_key = get_key(linked_issue)
        if should_ignore_issue(linked_issue):
            if jira_options.verbose:
                log('Skipping ' + linked_issue_key + ' - explicitly excluded')
            return

        link_type = link['type'][direction]

        if jira_options.ignore_states and link_issue_direction and (link[link_issue_direction]['fields']['status']['name'] in jira_options.ignore_states):
            if jira_options.verbose:
                log('Skipping ' + link_issue_direction + ' ' + linked_issue_key + ' - linked key is Closed')
            return

        if jira_options.includes not in linked_issue_key:
            return

        if link_type.strip() in jira_options.excludes:
            return linked_issue_key, None

        arrow = ' => ' if direction == 'outward' else ' <= '
        if jira_options.verbose:
            log(issue_key + arrow + link_type + arrow + linked_issue_key)

        extra = get_extra_decorations_for_link_type(link_type)
        if direction not in jira_options.show_directions:
            edge_definition = None
        else:
            if jira_options.verbose:
                log(create_node_name(issue_key, fields))
            edge_definition = '{}->{}[label="{}"{}]'.format(
                create_node_text(issue_key, fields),
                create_node_text(linked_issue_key, linked_issue['fields']),
                '',
                extra)

        return linked_issue_key, edge_definition

    def add_node_to_graph(graph, issue_key, fields, islink = False):
        node_text = create_node_text(issue_key, fields, islink=False)
        if islink:
            add_link_to_graph(graph, node_text)
        else:
            graph.add_issue_node(node_text)
        return node_text

    def add_link_to_graph(graph, line_definition):
        graph.add_link_node(line_definition)
        return line_definition

    def walk(issue_key, graph):
        fields = jira.get_mapped_issue_fields(issue_key)

        graph.mark_as_seen(issue_key)

        children = []

        if jira_options.ignore_states and (fields['status']['name'] in jira_options.ignore_states):
            if jira_options.verbose:
                log('Skipping ' + issue_key + ' - state is one of ' + ','.join(jira_options.ignore_states))
            return graph

        if not jira_options.traverse and ((project_prefix + '-') not in issue_key):
            if jira_options.verbose:
                log('Skipping ' + issue_key + ' - not traversing to a different project')
            return graph

        add_node_to_graph(graph, issue_key, fields, islink = False)

        issue_type = get_issue_type(fields)
        issue_name = create_node_name(issue_key, fields)

        if True: #not ignore_subtasks:
            if issue_type == 'Epic' and not jira_options.ignore_epic:
                issues = jira.query('"Epic Link" = "%s"' % issue_key)
                for subtask in issues:
                    if not should_ignore_issue(subtask):
                        subtask_key = get_key(subtask)
                        if jira_options.verbose:
                            log(subtask_key + ' => references => ' + issue_name)
                        link_text = '{}->{}[color=orange]'.format(
                            create_node_text(issue_key, fields),
                            create_node_text(subtask_key, subtask['fields']))
                        add_link_to_graph(graph, link_text)
                        children.append(subtask_key)
            if 'subtasks' in fields and not jira_options.ignore_subtasks:
                for subtask in fields['subtasks']:
                    if not should_ignore_issue(subtask):
                        subtask_key = get_key(subtask)
                        subtask_name = create_node_name(subtask_key, subtask['fields'])
                        if jira_options.verbose:
                            log(issue_name + ' => has subtask => ' + subtask_name)
                        link_text = '{}->{}[color=blue][label="subtask"]'.format (
                                create_node_text(issue_key, fields),
                                create_node_text(subtask_key, subtask['fields']))
                        add_link_to_graph(graph, link_text)
                        children.append(subtask_key)

        if 'issuelinks' in fields:
            for other_link in fields['issuelinks']:
                # conditionally generate the link between issue_key and the other_link
                # this will be an edge or directed-line in the visualization, and does
                # not define the nodes in question
                result = process_link(fields, issue_key, other_link)
                if result is not None:
                    if jira_options.verbose:
                        log('Appending ' + result[0])
                    children.append(result[0])
                    if result[1] is not None:
                        graph.add_link_node(result[1])
    
        # now construct graph data for all children, which could be:
        #   items in the epic
        #   subtasks
        #   links to/from this issue
        for child in (x for x in children if not graph.has_seen(x)):
            walk(child, graph)
        return graph


    project_prefix = start_issue_key.split('-', 1)[0]

    return walk(start_issue_key, graph)



def parse_args():
    parser = argparse.ArgumentParser(conflict_handler='resolve')
    parser.add_argument('-u', '--user', dest='user', default=None, help='Username to access JIRA')
    parser.add_argument('-p', '--password', dest='password', default=None, help='Password to access JIRA')
    parser.add_argument('-c', '--cookie', dest='cookie', default=None, help='JSESSIONID session cookie value')
    parser.add_argument('-j', '--jira', dest='jira_url', default='http://jira.example.com', help='JIRA Base URL (with protocol)')
    parser.add_argument('-f', '--file', dest='image_file', default='issue_graph.png', help='Filename to write image to')
    parser.add_argument('-l', '--local', action='store_true', default=False, help='Render graphviz code to stdout')
    parser.add_argument('-e', '--ignore-epic', action='store_true', default=False, help='Don''t follow an Epic into it''s children issues')
    parser.add_argument('-x', '--exclude-link', dest='excludes', default=[], action='append', help='Exclude link type(s)')
    parser.add_argument('-it', '--ignore-type', dest='ignore_types', action='append', default=[], help='Ignore issues of this type')
    parser.add_argument('-is', '--ignore-state', dest='ignore_states', action='append', default=[], help='Ignore issues with this state')
    parser.add_argument('-i', '--issue-include', dest='includes', default='', help='Include issue keys')
    parser.add_argument('-xi', '--issue-exclude', dest='issue_excludes', action='append', default=[], help='Exclude issue keys; can be repeated for multiple issues')
    parser.add_argument('-s', '--show-directions', dest='show_directions', default=['inward', 'outward'], help='which directions to show (inward, outward)')
    parser.add_argument('-d', '--directions', dest='directions', default=['inward', 'outward'], help='which directions to walk (inward, outward)')
    parser.add_argument('-ns', '--node-shape', dest='node_shape', default='box', help='which shape to use for nodes (circle, box, ellipse, etc)')
    parser.add_argument('-t', '--ignore-subtasks', action='store_true', default=False, help='Don''t include sub-tasks issues')
    parser.add_argument('-T', '--dont-traverse', dest='traverse', action='store_false', default=True, help='Do not traverse to other projects')
    parser.add_argument('-w', '--word-wrap', dest='word_wrap', default=False, action='store_true', help='Word wrap issue summaries instead of truncating them')
    parser.add_argument('-v', '--verbose', dest='verbose', default=False, action='store_true', help='Verbose logging')
    parser.add_argument('-af', '--add-field', dest='extra_fields', action='append', default=[], help='Include these extra fields from the issues, such as "Epic Link"')
    parser.add_argument('-la', '--label', dest='labels', action='append', default=[], help='Find these labels (ex: "B&P_Ingestion")')

    parser.add_argument('--no-verify-ssl', dest='no_verify_ssl', default=False, action='store_true', help='Don\'t verify SSL certs for requests')
    parser.add_argument('issues', nargs='+', help='The issue key (e.g. JRADEV-1107, JRADEV-1391)')

    return parser.parse_args([
        '--user', 'gtempel@billtrust.com',
        '--password', 'QZ12rb4a5VEyBPwwOxZS8C27',
        '--jira', 'https://billtrust.atlassian.net',
        '--ignore-state', 'Closed',
        '--ignore-state', 'Done',
        '--ignore-state', 'Deployed',
        '--ignore-state', 'Completed',
        '--ignore-state', 'Rolled',
        '--exclude-link', 'clones',
        '--exclude-link', 'is cloned by',
        '--exclude-link', 'is blocked by',
        '--exclude-link', 'is related to',
        '--issue-include', 'ARC',
        '--ignore-type', 'Certified',
        '--ignore-type', 'Bug',
        '--ignore-type', 'Test',
        '--ignore-subtasks', 
        '--add-field', 'Epic Link',
        '--add-field', 'labels',
        '--label', 'B&P',
        '--verbose', 
        'ARC-5164'
        ]
    )




def main():
    options = parse_args()

    if options.cookie is not None:
        # Log in with browser and use --cookie=ABCDEF012345 commandline argument
        auth = options.cookie
    else:
        # Basic Auth is usually easier for scripts like this to deal with than Cookies.
        user = options.user if options.user is not None \
                    else input('Username: ')
        password = options.password if options.password is not None \
                    else getpass.getpass('Password: ')
        auth = (user, password)

    jira = JiraSearch(options.jira_url, auth, options.no_verify_ssl, options.extra_fields)
    graph = JiraGraph()

    jira_options = JiraOptions(vars(options))

    labels = jira.get_labels(jira_options.labels)
    if labels and jira_options.verbose:
        print(labels)

    for issue in jira_options.issues:
        build_graph_data(graph, issue, jira, jira_options)
   
    if jira_options.local:
        print(graph.generate_digraph(jira_options.node_shape))
    else:
        graph_renderer = JiraGraphRenderer()
        graph_renderer.generate_dotfile(graph, jira_options.node_shape, 'graph_data.dot')
        graph_renderer.render(graph, 'issue_graph.png')



if __name__ == '__main__':
    main()
