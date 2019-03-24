from notion.client import NotionClient
from notion.markdown import notion_to_markdown
import notion
import os
import sys
import errno
import asyncio
import json
from datetime import date
from itertools import chain


synced_files = dict()


def rm_file(filepath):
    filestat = os.stat(filepath)
    if filestat is not None and filestat.is_file():
        os.remove(filepath)


def init():
    with open('./config.json') as config_file:
        config = json.load(config_file)
        client = NotionClient(token_v2=config['token_v2'])
        return (
            client,
            client.get_collection_view(config['sync_root']),
            config['destination']
        )


def get_post_meta(row):
    tags = chain(*[
        row.get_property(entry['id'])
        for entry in row.schema
        if (entry['name'] == "Tags"
            and entry['type'] == 'multi_select')
    ])
    return '---\ntitle: %s\ntags: %s\n---' % (
        get_decorated_row_title(row), ', '.join(tags))


def get_row_publish_date(row):
    publish_dates = [
        row.get_property(entry['id'])
        for entry in row.schema
        if (entry['name'] == "Publish Date"
            and entry['type'] == 'date')
    ]
    dates = [
        publish_date.start for publish_date in publish_dates if publish_date is not None]
    return None if len(dates) == 0 else max(dates)


def set_row_status(row, value):
    for entry in row.schema:
        if entry['name'] == "Status":
            row.set_property(entry['id'], value)


def set_row_published_pending(row):
    '''
    Sets a row's status to either Published, Pending, or None based on
    if it has an assigned publish date
    '''
    publish_date = get_row_publish_date(row)
    if publish_date is None:
        set_row_status(row, 'Unpublished')
    elif publish_date > date.today():
        set_row_status(row, 'Pending')
    else:
        set_row_status(row, 'Published')


def is_row_published(row):
    is_published_status = any([
        row.get_property(entry['id']) in 'Published' for entry in row.schema if entry['name'] == "Status"
    ])

    return is_published_status


def get_row_link_slug(row):
    publish_date = get_row_publish_date(row)
    if publish_date is None:
        return None

    return '-'.join(
        ["%04d-%02d-%02d" % (
            publish_date.year,
            publish_date.month,
            publish_date.day,
        )] +
        row.title.split(' ')
    )


class CollectionGeneratorContext:

    def __init__(self, collection_generator):
        self.collection_generator = collection_generator

    def contains_row(self, block):
        # Explicitly opt not to support embedded subpages
        # (e.g. subpages that are _indirect_ descendents of the collection)
        is_block_in_root_collection = block.collection.id == self.collection_generator.collection.id
        return (
            is_block_in_root_collection
            and isinstance(block, notion.collection.CollectionRowBlock)
            and is_row_published(block)
        )

    def get_block_url(self, block):
        return '/posts/' + get_row_link_slug(block)


def get_decorated_row_title(block):
    return block.title if block.icon is None else '%s %s' % (
        block.icon,
        block.title
    )


class MarkdownGenerator:

    def __init__(self, context):
        self.context = context

    def get_markdown_from_page(self, block, is_page_root=False):
        # print('traverse', type(block), block)
        if isinstance(block, notion.collection.CollectionRowBlock):
            if is_page_root:
                # if we are on the page root, traverse the subpage
                return '\n\n'.join([md for md in [
                    self.get_markdown_from_page(child)
                    for child in block.children
                ] if md is not None])
            else:
                # otherwise, just link to the page

                contains_row = self.context.contains_row(block)
                if not contains_row:
                    print('contains block?', contains_row)
                    return ''

                block_url = self.context.get_block_url(block)

                return '[%s](%s)' % (
                    get_decorated_row_title(block),
                    block_url
                )

        elif isinstance(block, notion.block.TextBlock):
            return block.title
        elif isinstance(block, notion.block.HeaderBlock):
            return '# ' + block.title
        elif isinstance(block, notion.block.SubheaderBlock):
            return '## ' + block.title
        elif block.type == 'sub_sub_header':
            return '### ' + \
                notion_to_markdown(
                    block._get_record_data()['properties']['title'])
        elif isinstance(block, notion.block.BulletedListBlock):
            row = '- ' + block.title
            subrows = self.indent_children(block.children)
            return row + '\n' + subrows
        elif isinstance(block, notion.block.NumberedListBlock):
            row = '1. ' + block.title
            subrows = self.indent_children(block.children)
            return row + '\n' + subrows
        elif isinstance(block, notion.block.ColumnListBlock):
            subsections = '\n'.join([
                self.get_markdown_from_page(child)
                for child in block.children
            ])
            return '<section class="columnSplit" style="display:flex;">\n%s\n</section>' % subsections
        elif isinstance(block, notion.block.ColumnBlock):
            return '<section style="flex: %s">\n%s\n</section>' % (
                block.column_ratio,
                '\n'.join(self.get_markdown_from_page(child)
                          for child in block.children)
            )
        elif isinstance(block, notion.block.ImageBlock):
            raw_source = notion_to_markdown(
                block._get_record_data()['properties']['source'])
            return '![](%s)' % (
                # os.path.basename(raw_source),
                block.source
            )
        elif isinstance(block, notion.block.CodeBlock):
            code_source = block.title
            code_language = block.language
            return '```%s\n%s\n```' % (
                code_language,
                code_source
            )
        elif isinstance(block, notion.block.QuoteBlock):
            quote_body = block.title
            return '> ' + '\n> '.join(quote_body.split('\n'))
        elif isinstance(block, notion.block.TodoBlock):
            row = '[%s] %s' % ('x' if block.checked else ' ', block.title)
            subrows = self.indent_children(block.children)
            return row + '\n' + subrows
        elif isinstance(block, notion.block.DividerBlock):
            return '---\n'
        elif isinstance(block, notion.block.CollectionViewBlock):
            # TODO handle these if they are tables
            pass
        else:
            print('encountered unknown block type')
            print(type(block), block, block._get_record_data())
            return str(block)

    def indent_children(self, children):
        return ''.join([
            '  ' + md.replace('\n', '\n  ')
            for md in
            [
                self.get_markdown_from_page(child)
                for child in children
            ]
            if md is not None
        ])


class RowSync:
    '''
    Synchronizes row's content to a markdown file
    '''

    def __init__(self, root_dir, row, markdown_generator):
        self.root_dir = root_dir
        self.row = row
        self.markdown_generator = markdown_generator
        self.filename = self._get_sync_filename()

    def start(self):
        self.callback_id = self.row.add_callback(self.update_file)
        self.update_file()

    def update_file(self):
        # Make sure the data on the row is consistent
        set_row_published_pending(self.row)

        if (self.filename != self._get_sync_filename()):
            print('removing old file at ', self.filename)
            rm_file(self.filename)
            self.filename = self._get_sync_filename()

        if (is_row_published(self.row)):
            print('row updated, writing file', self.filename)
            with open(self.filename, 'w') as file_handle:
                meta = get_post_meta(self.row)
                file_handle.write(
                    meta +
                    '\n\n' +
                    self.markdown_generator.get_markdown_from_page(
                        self.row,
                        is_page_root=True
                    )
                )
        elif os.path.exists(self.filename):
            filestat = os.stat(self.filename)
            if filestat is not None and filestat.is_file():
                rm_file(self.filename)

    def remove_and_stop(self):
        self.row.remove_callbacks(self.callback_id)
        os.remove(self.filename)

    def _get_sync_filename(self):
        # TODO format based on date of the entry
        return "%s/%s.md" % (self.root_dir, get_row_link_slug(self.row))


class CollectionFileSync:
    '''
    Synchronizes a collection's rows to individual markdown files

    Tracks row addition / removal
    '''

    def __init__(self, collection, root_dir):
        self.collection = collection
        self.root_dir = root_dir
        self.markdown_generator = MarkdownGenerator(
            CollectionGeneratorContext(self)
        )

        self.known_rows = dict()

    def start(self):
        self.callback = self.collection.add_callback(self.sync_rows)
        self.sync_rows()

    def stop(self):
        self.collection.add_callback(self.sync_rows)
        self.sync_rows()

    def sync_rows(self):
        print('syncing rows!')
        rows = self.collection.get_rows()
        rows_dict = dict((row.id, row) for row in rows)
        new_row_ids = frozenset(row.id for row in rows)
        old_row_ids = self.known_rows.keys()

        added_row_ids = new_row_ids - old_row_ids
        removed_row_ids = old_row_ids - new_row_ids

        print("    added", added_row_ids, "removed", removed_row_ids)

        for added_row_id in added_row_ids:
            row_sync = RowSync(
                self.root_dir,
                rows_dict[added_row_id],
                self.markdown_generator
            )
            self.known_rows[added_row_id] = row_sync
            row_sync.start()

        for removed_row_id in removed_row_ids:
            self.known_rows[removed_row_id].remove_and_stop()
            del self.known_rows[removed_row_id]


async def main():
    print('reading config')
    client, root_view, destination_dir = init()
    print('making out dir')
    os.makedirs(destination_dir, exist_ok=True)

    print('got root')
    sync = CollectionFileSync(root_view.collection, destination_dir)
    print('starting sync')
    sync.start()

    print('entering indefinite wait')
    while True:
        sys.stdout.flush()
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
