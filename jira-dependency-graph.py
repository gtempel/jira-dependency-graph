#!/usr/bin/env python

from __future__ import print_function

import argparse
import getpass
import sys
import requests
import dateparser
import re
from datetime import datetime
from more_itertools import bucket
import cardinality

GOOGLE_CHART_URL = 'https://chart.apis.google.com/chart'
MAX_SUMMARY_LENGTH = 30


def log(*args):
    print(*args, file=sys.stderr)

class JiraGraph(object):
    """ This object holds the graph data for the nodes we create while we
        traverse the Jira cases and links. It's providing a wrapper around the specific
        method of storage so we can abstract it.
    """
    __graph_data = { 'nodes': set(), 'links': set() }
    __seen = set()
    __blocked = set()

    def add_issue_node(self, node):
        self.__graph_data['nodes'].add(node)
        if node.blocked():
            self.add_blocked_node(node)

    def add_link_node(self, node):
        self.__graph_data['links'].add(node)

    def mark_as_seen(self, issue_key):
        self.__seen.add(issue_key)
    
    def has_seen(self, issue_key):
        return issue_key in self.__seen

    def add_blocked_node(self, node):
        self.__blocked.add(node)

    def generate_digraph(self, options):
        """
            This method takes the graph information and converts it to dot (graphviz) notation,
            returning the dot description as a string to the caller.
        """
        ranks = []
        if options.grouped:
            buckets = bucket(self.__graph_data['nodes'], key=lambda x: x.get_date().strftime("%Y%m%d") if x.get_date() else '')
            bucket_list = sorted(list(buckets))
            sorted_buckets = sorted(list(buckets))
            for k in sorted_buckets:
                items = sorted([node.create_node_name() for node in list(buckets[k])])
                label = k if k else 'None'
                ranks.append('subgraph cluster_' + label + ' {\nlabel="' + label + '"\nrank=same\n"' + '",\n"'.join(items) + '"\n};')

        nodes = ';\n'.join(sorted([node.create_node_text() for node in self.__graph_data['nodes']]))
        links = ';\n'.join(sorted(self.__graph_data['links']))
        blockers = ';\n'.join(['"{}" [color=red, penwidth=2]'.format(node.create_node_name()) for node in self.__blocked])
        
        dates = list(set(node.get_date() for node in self.__graph_data['nodes'] if node.get_date()))
        counts = {
            'Story': 0,
            'Epic': 0,
            'Cert': 0
        }
        count_blockers = len(self.__blocked)

        for node in self.__graph_data['nodes']:
            for key, value in counts.items():
                if node.is_type(key):
                    counts[key] += 1
        
        graph_label = []
        graph_label.append("Generated @ " + datetime.now().replace(microsecond=0).isoformat(' '))
        if dates:
            start_date = min(dates)
            end_date = max(dates)
            if start_date:
                graph_label.append("Starting " + start_date.strftime("%Y-%m-%d"))
            if end_date:
                graph_label.append("Ending " + end_date.strftime("%Y-%m-%d"))
        if options.issues:
            graph_label.append("Cases: " + ', '.join(options.issues))
        if options.labels:
            graph_label.append("Cases labeled: " + ', '.join(options.labels))
        if options.extra_fields:
            graph_label.append("Showing: " + ', '.join(options.extra_fields))
        if options.grouped:
            graph_label.append("Grouped by date (sprint end or CERT implementation date")
        
        for key, count in counts.items():
            if counts[key]:
                graph_label.append(key + ': ' + str(counts[key]))
        if count_blockers:
            graph_label.append('Blockers: ' + str(count_blockers))
        
        digraph = "digraph Dependencies {\n" + \
            'graph [fontname=Helvetica];\n' + \
            'labelloc=top; labeljust=left;\n' + \
            'label="' + '\l'.join(graph_label) + '\l";\n' + \
            'node [fontname=Helvetica, shape=' + options.node_shape +'];' + '\n' + \
            'graph [rankdir=LR];\n' + \
            '// Graph starts here\n' + \
            '// Nodes\n' + nodes + '\n' + \
            '// Edges (links)\n' + links + '\n' + \
            '// These items are blocked\n' + blockers + '\n' + \
            '// Grouped by date\n' + "\n".join(ranks) + '\n' + \
            '}'
        return digraph

class JiraGraphRenderer(object):
    """ Refactored rendering information from the JiraGraph to here. This class'
        responsibilities are rendering the graph to a dot (graphviz) file as well
        as (potentially) a png via a web service call (not working at the moment)
    """

    def generate_dotfile(self, graph, options, filename='graph_data.dot'):
        """
            Given the graph object, ask it to be rendered to a dot file
            then write that file to storage using the given filename.
        """
        digraph = graph.generate_digraph(options)
        with open(filename, "w") as dotfile:
            dotfile.write(digraph)
            dotfile.close()
        return digraph

    def render(self, graph, options, filename='issue_graph.png'):
        """ Given a formatted blob of graphviz chart data[1], make the actual request to Google
            and store the resulting image to disk.

            [1]: http://code.google.com/apis/chart/docs/gallery/graphviz.html
        """
        digraph = graph.generate_digraph(options)
        print('sending: ', GOOGLE_CHART_URL, {'cht':'gv', 'chl': digraph})

        response = requests.post(GOOGLE_CHART_URL, data = {'cht':'gv', 'chl': digraph})

        with open(filename, 'w+b') as image:
            print('Writing to ' + filename)
            binary_format = bytearray(response.content)
            image.write(binary_format)
            image.close()
        return filename


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
            else:
                fieldnames.append(extra_issue_field_name)
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

    def get_issues_with_labels(self, labels_to_find):
        issues = []
        if labels_to_find:
            response = self.query('labels in ({labels})'.format(labels=','.join('"' + item + '"' for item in labels_to_find)))
            issues = [item['key'] for item in response]
        return issues

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
                new_labels = [label for label in payload['values'] if any(sub in label for sub in labels_to_find)]
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
            if k in fields.keys():
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


class JiraNode(object):
    __fields = {}
    __key = None
    __blocked = False
    __uri = None

    def __init__(self, issue_key = None, fields = {}, uri = None):
        self.__key = issue_key
        self.__fields = fields
        self.__uri = uri # jira.get_issue_uri(issue_key)
        self.__blocked = 'BLOCK' in self.status_text()

    def __eq__(self, other):
        """Overrides the default implementation"""
        if isinstance(other, JiraNode):
            return self.key() == other.key()
        return NotImplemented

    def __hash__(self):
        """Overrides the default implementation"""
        return hash(self.key())

    def key(self):
        return self.__key

    def fields(self):
        return self.__fields

    def block(self, block_or_blocked = False):
        self.__blocked |= block_or_blocked
        return self.blocked()

    def blocked(self):
        return self.__blocked

    def status(self):
        return self.__fields['status']

    def status_text(self):
        return self.status()['statusCategory']['name'].upper()

    def is_status_ignore(self, ignore_states = []):
        return self.status_text() in ignore_states

    def status_color(self):
        default_color = 'white'
        colors = {
            'IN PROGRESS': 'yellow',
            'DONE': 'green',
            'BLOCKED': 'red',
            'BLOCKS' : 'red'
        }
        status = self.status_text()
        color = colors.get(status, default_color)
        return color

    def get_extra_decorations_for_status(self):
        return ''

    def is_type(self, *issue_types):
        this_type = self.issue_type().upper()
        return any(t.upper() in this_type for t in issue_types)

    def is_epic(self):
        return self.is_type('EPIC')

    def is_cert(self):
        return self.is_type('CERT')

    def issue_type(self):
        issue_type = self.fields()['issuetype']['name']
        return issue_type

    def labels(self):
        try:
            return self.fields()['labels']
        except (KeyError, ValueError, TypeError):
            return None

    def team_name(self):
        key = 'Team Name'
        try:
            return self.fields()[key][0]['value']
        except (KeyError, ValueError, TypeError):
            return None
    
    def sprint(self):
        key = 'Sprint'
        try:
            return self.fields()[key][0]['name']
        except (KeyError, ValueError, TypeError):
            return None
    
    def sprint_end_date(self):
        key = 'Sprint'
        try:
            return self.fields()[key][0]['endDate']

        except (KeyError, ValueError, TypeError):
            # likely Jira doesn't have Sprint detail info because it hasn't started yet, so we'll
            # have to mine what we have
            regex = r"(\d{1,2}\/\d{1,2}\/?(?:\d{0,4})){0,1}\s*-\s*(\d{1,2}\/\d{1,2}\/?(?:\d{0,4})){0,1}(?:\s*\('?(\d{0,4})\)){0,1}"
            text = self.sprint()
            end_date = None
            if text:
                match = re.search(regex, text)
                groups = match.groups() if match else (None, None)

                group_count = len(groups)
                year_group = groups[2] if (group_count == 3) else None
                if year_group:
                    end_date = groups[1] + '/' + year_group
                else:
                    end_date = groups[1]

            return end_date


    def cab_datetime(self):
        key = 'Implementation Date/Time'
        try:
            datetime = self.fields()[key]
            datetime = datetime.split('T', 1)
            return datetime[0]
        except (KeyError, ValueError, TypeError, AttributeError):
            return None
    
    def get_date(self):
        # have to examine either the cab_datetime or the date parsed from the sprint
        cab_info = self.cab_datetime()
        sprint_info = self.sprint_end_date()

        if cab_info:
            return dateparser.parse(cab_info).date()
        elif sprint_info:
            return dateparser.parse(sprint_info).date()
        else:
            return None

    def shape(self):
        default_shape='rect'
        shapes = {
            "Epic": "oval", #"diamond",
            "Story": default_shape,
            "Spike": default_shape,
            "subtask": "text", #"oval",
            "Task": "MCircle",
            "Certified": "box3d"
        }

        issue_type = self.issue_type()
        shape = shapes.get(issue_type, default_shape)
        return shape

    def get_subtasks(self):
        key = 'subtasks'
        return self.__fields[key] if key in self.__fields else []

    def get_issue_links(self):
        key = 'issuelinks'
        return self.__fields[key] if key in self.__fields else []

    def create_node_name(self):
        no_issue_type_prefixes = [
            'Story',
            'Certified',
            'Task',
            'ACL (Access Control Language)'
        ]
        
        issue_type = self.issue_type()
        if issue_type in no_issue_type_prefixes:
            return self.key()
        return '{} {}'.format(issue_type, self.key())


    def create_node_description(self):
        labels = self.labels()
        if labels:
            labels = '\\n'.join(['#' + label for label in labels if label])
        else:
            labels = None

        parts = [ self.create_node_name(),
                self.team_name(),
                self.sprint(),
                self.cab_datetime(),
                self.get_node_summary(),
                labels
                ]
        description = '\\n'.join([x for x in parts if x is not None])
        return description
    
    def get_node_summary(self):
        summary = self.__fields['summary']

        # truncate long labels with "...", but only if the three dots are replacing more than two characters
        # -- otherwise the truncated label would be taking more space than the original.
        if len(summary) > MAX_SUMMARY_LENGTH + 2:
            summary = summary[:MAX_SUMMARY_LENGTH] + '...'
        summary = summary.replace('"', "'")
        return summary

    def create_node_text(self, islink=False):
        issue_name = self.create_node_name()

        if islink:
            summary = self.get_node_summary()
            return '"{}\\n({})"'.format(issue_name, summary)
        
        return '"{}" [label="{}", shape="{}", href="{}", fillcolor="{}", style=filled {}]'.format(issue_name, 
                                                                                            self.create_node_description(),
                                                                                            self.shape(), 
                                                                                            self.__uri, 
                                                                                            self.status_color(),
                                                                                            self.get_extra_decorations_for_status())


def build_graph_data(graph,
                     start_issue_key, 
                     jira, 
                     jira_options):
                     
    """ Given a starting image key and the issue-fetching function build up the GraphViz data representing relationships
        between issues. This will consider both subtasks and issue links, among other things, as per the jira_options.
    """


    def get_extra_decorations_for_link_type(link_type):
        extra = ',color="red", penwidth=4.0' if 'BLOCK' in link_type.upper() else ""
        return extra

    def should_ignore_issue(node):
        issue_key = node.key()
        issue_type = node.issue_type()
        if (jira_options.issue_excludes and (issue_key in jira_options.issue_excludes)) or (jira_options.ignore_types and (issue_type in jira_options.ignore_types)):
            if jira_options.verbose: log('Issue ' + issue_key + ' - should be ignored')
            return True
        if should_ignore_issue_due_to_state(node):
            return True

        return False


    def should_ignore_issue_due_to_state(node):
        if node.is_status_ignore(jira_options.ignore_states):
            issue_key = node.key()
            if jira_options.verbose: log('Skipping ' + issue_key + ' - state is one of ' + ','.join(jira_options.ignore_states))
            return True
        return False


    def process_link(node, link):
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
        linked_node = JiraNode(linked_issue['key'], link[link_issue_direction]['fields'], jira.get_issue_uri(linked_issue['key']))
        linked_issue_key = linked_node.key()
        if should_ignore_issue(linked_node):
            return

        link_type = link['type'][direction]

        if jira_options.ignore_states and link_issue_direction and (link[link_issue_direction]['fields']['status']['name'] in jira_options.ignore_states):
            return

        if jira_options.includes not in linked_issue_key:
            return

        if link_type.strip() in jira_options.excludes:
            return linked_issue_key, None

        arrow = ' => ' if direction == 'outward' else ' <= '
        if jira_options.verbose: log(node.key() + arrow + link_type + arrow + linked_issue_key)

        extra = get_extra_decorations_for_link_type(link_type)
        if direction not in jira_options.show_directions:
            edge_definition = None
        else:
            if jira_options.verbose: log(node.create_node_name())
            edge_definition = '"{}"->"{}"[label="{}"{}]'.format(
                node.create_node_name(),
                linked_node.create_node_name(),
                link_type if link_type in jira_options.link_labels else '',
                extra)
        blocked = 'BLOCK' in link_type.upper()
        if blocked:
            node.block(blocked)
            linked_node.block(blocked)
            graph.add_blocked_node(node)
            graph.add_blocked_node(linked_node)

        return linked_issue_key, edge_definition

    def add_node_to_graph(graph, node, islink = False):
        if islink:
            node_text = node.create_node_text(islink=False)
            add_link_to_graph(graph, node_text)
        else:
            graph.add_issue_node(node)
        
        return

    def add_link_to_graph(graph, line_definition):
        graph.add_link_node(line_definition)
        return line_definition

    def get_node(issue_key):
        fields = jira.get_mapped_issue_fields(issue_key)
        node = JiraNode(issue_key, fields, jira.get_issue_uri(issue_key) )
        return node
    
    def walk(issue_key, graph):
        node = get_node(issue_key)
        graph.mark_as_seen(issue_key)

        children = []

        if node.is_status_ignore(jira_options.ignore_states):
            if jira_options.verbose: log('Skipping ' + issue_key + ' - state is one of ' + ','.join(jira_options.ignore_states))
            return graph

        if not jira_options.traverse and ((project_prefix + '-') not in issue_key):
            if jira_options.verbose: log('Skipping ' + issue_key + ' - not traversing to a different project')
            return graph

        add_node_to_graph(graph, node, islink = False)

        issues = jira.query('"Epic Link" = "%s"' % issue_key) if node.is_epic() and not jira_options.ignore_epic else []
        for subtask in issues:
            subtask_node = JiraNode(subtask['key'], subtask['fields'], jira.get_issue_uri(subtask['key']))
            if not should_ignore_issue(subtask_node):
                link_text = '"{}"->"{}"[color=orange]'.format(
                    node.create_node_name(),
                    subtask_node.create_node_name())
                add_link_to_graph(graph, link_text)
                children.append(subtask_node.key())

        subtasks = [] if jira_options.ignore_subtasks else node.get_subtasks()
        for subtask in subtasks:
            subtask_node = JiraNode(subtask['key'], subtask['fields'], jira.get_issue_uri(subtask['key']))
            if not should_ignore_issue(subtask_node):
                subtask_name = subtask_node.create_node_name()
                link_text = '"{}"->"{}"[color=blue, label="subtask of"]'.format (
                    subtask_node.create_node_name(),
                    node.create_node_name())
                add_link_to_graph(graph, link_text)
                children.append(subtask_node.key())

        other_links = node.get_issue_links()
        for other_link in other_links:
            # conditionally generate the link between issue_key and the other_link
            # this will be an edge or directed-line in the visualization, and does
            # not define the nodes in question
            result = process_link(node, other_link)
            if result is not None:
                if jira_options.verbose: log('Appending ' + result[0])
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



def parse_args(arg_list = []):
    parser = argparse.ArgumentParser(conflict_handler='resolve')
    parser.add_argument('-u', '--user', dest='user', default=None, help='Username to access JIRA')
    parser.add_argument('-p', '--password', dest='password', default=None, help='Password to access JIRA')
    parser.add_argument('-c', '--cookie', dest='cookie', default=None, help='JSESSIONID session cookie value')
    parser.add_argument('-j', '--jira', dest='jira_url', default='http://jira.example.com', help='JIRA Base URL (with protocol)')
    parser.add_argument('-f', '--file', dest='image_file', default='issue_graph.png', help='Filename to write image to')
    parser.add_argument('-l', '--local', action='store_true', default=False, help='Render graphviz code to stdout')
    parser.add_argument('-e', '--ignore-epic', action='store_true', default=False, help='Don''t follow an Epic into it''s children issues')
    parser.add_argument('-x', '--exclude-link', dest='excludes', default=[], action='append', help='Exclude link type(s)')
    parser.add_argument('-ll', '--link-label', dest='link_labels', default=[], action='append', help='Provide labels for this type of relationship, such as "blocks"')
    parser.add_argument('-it', '--ignore-type', dest='ignore_types', action='append', default=[], help='Ignore issues of this type')
    parser.add_argument('-is', '--ignore-state', dest='ignore_states', action='append', default=[], help='Ignore issues with this state')
    parser.add_argument('-pi', '--project-include', dest='includes', default='', help='Include project keys')
    parser.add_argument('-px', '--project-exclude', dest='issue_excludes', action='append', default=[], help='Exclude these project keys; can be repeated for multiple issues')
    parser.add_argument('-s', '--show-directions', dest='show_directions', default=['inward', 'outward'], help='which directions to show (inward, outward)')
    parser.add_argument('-d', '--directions', dest='directions', default=['inward', 'outward'], help='which directions to walk (inward, outward)')
    parser.add_argument('-ns', '--node-shape', dest='node_shape', default='box', help='which shape to use for nodes (circle, box, ellipse, etc)')
    parser.add_argument('-t', '--ignore-subtasks', action='store_true', default=False, help='Don''t include sub-tasks issues')
    parser.add_argument('-T', '--dont-traverse', dest='traverse', action='store_false', default=True, help='Do not traverse to other projects')
    parser.add_argument('-v', '--verbose', dest='verbose', default=False, action='store_true', help='Verbose logging')
    parser.add_argument('-af', '--add-field', dest='extra_fields', action='append', default=[], help='Include these extra fields from the issues, such as "Epic Link"')
    parser.add_argument('-la', '--label', dest='labels', action='append', default=[], help='Find these labels (ex: "B&P_Ingestion")')
    parser.add_argument('-b', '--blockers', dest='blockers', default=False, action='store_true', help='Highlight blocking and blocked items')
    parser.add_argument('-g', '--grouped', dest='grouped', default=False, action='store_true', help='Group cases by dates (sprint end or CERT date)')

    parser.add_argument('--no-verify-ssl', dest='no_verify_ssl', default=False, action='store_true', help='Don\'t verify SSL certs for requests')
    parser.add_argument('issues', nargs='+', help='The issue key (e.g. JRADEV-1107, JRADEV-1391)')

    return parser.parse_args(arg_list)




def main(arg_list = []):

    options = parse_args(arg_list)

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

    # labels = jira.get_labels(jira_options.labels)
    # if labels and jira_options.verbose:
    #     print(labels)

    cases = jira.get_issues_with_labels(jira_options.labels)
    print(cases)

    jira_options.ignore_states = [ state.upper() for state in jira_options.ignore_states ]

    if not cases:
        cases = []

    jira_options.issues = [item for item in jira_options.issues if item]
    for issue in [item for item in jira_options.issues if item] + cases:
        build_graph_data(graph, issue, jira, jira_options)

    if jira_options.local:
        print(graph.generate_digraph(jira_options))
    else:
        graph_renderer = JiraGraphRenderer()
        graph_renderer.generate_dotfile(graph, jira_options, 'graph_data.dot')
        # graph_renderer.render(graph, jira_options, 'issue_graph.png')
    print("Done")


if __name__ == '__main__':
    arg_list = [
        '--user', 'gtempel@billtrust.com',
        '--password', 'QZ12rb4a5VEyBPwwOxZS8C27',
        '--jira', 'https://billtrust.atlassian.net',
        '--ignore-state', 'Closed',
        '--ignore-state', 'Done',
        '--ignore-state', 'Deployed',
        '--ignore-state', 'Not Deployed',
        # '--ignore-state', "Won't Do",
        '--ignore-state', 'Completed',
        '--ignore-state', 'Rolled',
        '--exclude-link', 'clones',
        '--exclude-link', 'is cloned by',
        '--exclude-link', 'is blocked by',
        '--exclude-link', 'is related to',
        # '--link-label', 'blocks',
        #'--project-include', 'ARC',
        #'--ignore-type', 'Certified',
        '--ignore-type', 'Bug',
        '--ignore-type', 'Test',
        # '--ignore-subtasks', 
        '--add-field', 'Epic Link',
        '--add-field', 'labels',
        '--add-field', 'Team Name',
        '--add-field', 'Implementation Date/Time',
        '--add-field', 'Sprint',
        '--label', 'colonial',
        #'--verbose', 
        '--blockers',
        # '--grouped',
        #'--local',
        'ARC-4982',
        'ARC-5658',
        'ARC-5168',
        'ARC-5420',
        ''
        ]
    main(arg_list)
