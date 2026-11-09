import { getDatabase } from '../../utils/databaseAdapter.js';
import { generateShortId } from '../../upload/uploadTools.js';
import { rebuildIndex } from '../../utils/indexManager.js';

const corsHeaders = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
};

export async function onRequest(context) {
    const { request, env } = context;

    if (request.method === 'OPTIONS') {
        return new Response(null, { headers: corsHeaders });
    }

    const db = getDatabase(env);
    if (!db) {
        return new Response(JSON.stringify({ success: false, error: 'Database not configured' }), {
            status: 500,
            headers: { 'Content-Type': 'application/json', ...corsHeaders }
        });
    }

    try {
        console.log('Starting migration to short IDs for historical files...');

        // 1. 获取数据库中所有文件记录
        const allFiles = [];
        let cursor = null;
        while (true) {
            const listRes = await db.list({ limit: 1000, cursor });
            if (listRes && Array.isArray(listRes.keys)) {
                for (const k of listRes.keys) {
                    if (k.name.startsWith('manage@') || k.name.startsWith('chunk_')) {
                        continue;
                    }
                    allFiles.push(k);
                }
            }
            cursor = listRes ? listRes.cursor : null;
            if (!cursor) break;
        }

        console.log(`Found ${allFiles.length} total files in database.`);

        // 2. 识别老格式文件：非纯 8 位无路径短 ID
        const isPureShortId = (id) => {
            return typeof id === 'string' && /^[a-zA-Z0-9]{8}$/.test(id);
        };

        const legacyFiles = allFiles.filter(f => !isPureShortId(f.name));
        console.log(`Identified ${legacyFiles.length} legacy files to migrate.`);

        const migrated = [];
        const errors = [];

        for (const item of legacyFiles) {
            const oldId = item.name;
            try {
                // 读取完整记录
                const record = await db.getWithMetadata(oldId);
                if (!record) {
                    console.warn(`File record not found for ${oldId}`);
                    continue;
                }

                const metadata = record.metadata || {};

                // 生成唯一的 8 位短 ID
                let newId = '';
                while (true) {
                    const candidate = generateShortId(8);
                    if (await db.get(candidate) === null) {
                        newId = candidate;
                        break;
                    }
                }

                // 物理对象平移（Cloudflare R2）
                if (env.img_r2 && typeof env.img_r2.get === 'function') {
                    const r2Obj = await env.img_r2.get(oldId);
                    if (r2Obj) {
                        await env.img_r2.put(newId, r2Obj.body, {
                            httpMetadata: r2Obj.httpMetadata,
                            customMetadata: r2Obj.customMetadata
                        });
                        // 物理对象平移完毕，清理原对象
                        await env.img_r2.delete(oldId);
                    }
                }

                // 维护元数据：规范化保留 Directory 和 FileName
                if (!metadata.FileName) {
                    metadata.FileName = oldId.split('/').pop();
                }
                if (metadata.Directory === undefined || metadata.Directory === null) {
                    const lastSlash = oldId.lastIndexOf('/');
                    metadata.Directory = lastSlash !== -1 ? oldId.substring(0, lastSlash + 1) : '';
                }

                // 写入新 D1 记录并删除旧记录
                await db.put(newId, record.value || '', { metadata });
                await db.delete(oldId);

                // 建立永久重定向指针与别名（确保老链接 100% 永不断链）
                // 1) 完整老路径重定向：manage@redirect@{oldId} -> newId
                await db.put('manage@redirect@' + oldId, newId);
                // 2) 老文件名别名：manage@alias@{oldBaseName} -> newId
                const oldBaseName = oldId.split('/').pop();
                if (oldBaseName) {
                    await db.put('manage@alias@' + oldBaseName, newId);
                }
                // 3) 新短 ID 别名
                await db.put('manage@alias@' + newId, newId);

                migrated.push({ oldId, newId });
            } catch (err) {
                console.error(`Failed to migrate file ${oldId}:`, err);
                errors.push({ oldId, error: err.message });
            }
        }

        // 3. 触发全量主索引重建
        let rebuildResult = null;
        if (migrated.length > 0) {
            rebuildResult = await rebuildIndex(context);
        }

        return new Response(JSON.stringify({
            success: true,
            total: allFiles.length,
            migratedCount: migrated.length,
            failedCount: errors.length,
            migrated,
            errors,
            rebuild: rebuildResult
        }), {
            headers: { 'Content-Type': 'application/json', ...corsHeaders }
        });

    } catch (e) {
        console.error('Migration failed:', e);
        return new Response(JSON.stringify({ success: false, error: e.message }), {
            status: 500,
            headers: { 'Content-Type': 'application/json', ...corsHeaders }
        });
    }
}
