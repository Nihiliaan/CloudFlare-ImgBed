import assert from 'node:assert/strict';
import { sqliteDatabase } from './helpers/database.js';
import { rebuildIndex, readIndex } from '../functions/utils/indexManager.js';

describe('D1 database adapter', () => {
    it('pages through settings with a cursor instead of stopping at the limit', async () => {
        const { db } = await sqliteDatabase();
        // Session keys sort before sysConfig keys, so a single capped page would miss them.
        for (let i = 0; i < 5; i++) {
            await db.put(`manage@session@${i}`, '{}');
        }
        await db.put('manage@sysConfig@upload', '{"ok":true}');

        const names = [];
        let cursor = null;
        let pages = 0;
        do {
            const result = await db.list({ prefix: 'manage@', limit: 2, cursor });
            names.push(...result.keys.map(key => key.name));
            assert.equal(result.list_complete, !result.cursor);
            cursor = result.cursor;
            pages++;
        } while (cursor);

        assert.equal(pages, 3);
        assert.deepEqual(names.slice(-1), ['manage@sysConfig@upload']);
        assert.equal(new Set(names).size, 6);
    });

    it('keeps a single page complete when the settings fit within the limit', async () => {
        const { db } = await sqliteDatabase();
        await db.put('manage@sysConfig@upload', '{}');
        const result = await db.list({ prefix: 'manage@', limit: 1000 });
        assert.deepEqual(result.keys.map(key => key.name), ['manage@sysConfig@upload']);
        assert.equal(result.cursor, null);
        assert.equal(result.list_complete, true);
    });

    it('rebuilds index and recovers metadata directly from R2 physical storage when database is empty', async () => {
        const { sqlite, db } = await sqliteDatabase();
        const fakeR2 = {
            async list(options) {
                return {
                    objects: [
                        {
                            key: '2026/03/sample.png',
                            size: 1048576, // 1MB
                            uploaded: new Date(1741512000000),
                            httpMetadata: { contentType: 'image/png' }
                        },
                        {
                            key: 'root.jpg',
                            size: 2097152, // 2MB
                            uploaded: new Date(1741513000000),
                            httpMetadata: { contentType: 'image/jpeg' }
                        },
                        {
                            key: '.keep',
                            size: 0,
                            uploaded: new Date()
                        }
                    ],
                    truncated: false
                };
            }
        };

        const context = {
            env: {
                img_d1: sqlite,
                img_r2: fakeR2
            }
        };

        const result = await rebuildIndex(context);
        assert.equal(result.success, true);
        assert.equal(result.indexedCount, 2);

        // 验证 D1 数据库中已自动恢复写入对应元数据
        const file1 = await db.getFileWithMetadata('2026/03/sample.png');
        assert.ok(file1);
        assert.equal(file1.metadata.FileName, 'sample.png');
        assert.equal(file1.metadata.Directory, '2026/03/');
        assert.equal(file1.metadata.FileType, 'image/png');
        assert.equal(file1.metadata.FileSize, '1.00');
        assert.equal(file1.metadata.Channel, 'CloudflareR2');

        const file2 = await db.getFileWithMetadata('root.jpg');
        assert.ok(file2);
        assert.equal(file2.metadata.FileName, 'root.jpg');
        assert.equal(file2.metadata.Directory, '');
        assert.equal(file2.metadata.FileSize, '2.00');

        // 验证主索引读取成功：根目录下包含 1 个直接文件和 1 个直接子目录
        const rootResult = await readIndex(context);
        assert.equal(rootResult.success, true);
        assert.equal(rootResult.totalCount, 2);
        assert.equal(rootResult.files.length, 1);
        assert.equal(rootResult.files[0].id, 'root.jpg');
        assert.deepEqual(rootResult.directories, ['2026']);

        // 验证递归模式下全部 2 个文件都被读取
        const allResult = await readIndex(context, { includeSubdirFiles: true });
        assert.equal(allResult.success, true);
        assert.equal(allResult.files.length, 2);
    });
});
