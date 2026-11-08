import { getDatabase } from '../../../utils/databaseAdapter.js';
import { addFileToIndex, removeFileFromIndex } from "../../../utils/indexManager.js";
import { sanitizeUploadFolder } from "../../../upload/uploadTools.js";
import { purgeRandomFileListCache, purgePublicFileListCache } from "../../../utils/purgeCache.js";

// CORS 跨域响应头
const corsHeaders = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
    'Access-Control-Max-Age': '86400',
};

export async function onRequest(context) {
    const { request, env, params, waitUntil } = context;

    // OPTIONS 预检请求
    if (request.method === 'OPTIONS') {
        return new Response(null, {
            status: 204,
            headers: corsHeaders,
        });
    }

    const url = new URL(request.url);

    // 解析文件夹路径（支持从 params.path 或 JSON body/query 中获取）
    let folder = '';
    if (params?.path) {
        folder = decodeURIComponent(params.path).split(',').join('/');
    }

    if (!folder && request.method === 'POST') {
        try {
            const body = await request.json();
            if (body && typeof body.folder === 'string') {
                folder = body.folder;
            }
        } catch (e) {
            // body is not json or empty
        }
    }

    if (!folder) {
        folder = url.searchParams.get('folder') || '';
    }

    folder = sanitizeUploadFolder(folder);

    if (!folder) {
        return new Response(JSON.stringify({
            success: false,
            message: 'Folder path is required and cannot be empty.',
        }), {
            status: 400,
            headers: { 'Content-Type': 'application/json', ...corsHeaders },
        });
    }

    const markerId = `${folder}/.keep`;
    const db = getDatabase(env);

    // POST: 创建目录
    if (request.method === 'POST') {
        try {
            const metadata = {
                Channel: 'CloudflareR2',
                FileName: '.keep',
                FileType: 'application/octet-stream',
                FileSizeBytes: 0,
                Directory: `${folder}/`,
                IsDirectoryMarker: true,
                TimeStamp: Date.now(),
            };

            await db.put(markerId, '', { metadata });

            if (env.img_r2) {
                await env.img_r2.put(markerId, new Uint8Array(0));
            }

            waitUntil(addFileToIndex(context, markerId, metadata));

            // 清理列表缓存
            await purgeRandomFileListCache(url.origin, folder);
            await purgePublicFileListCache(url.origin, folder);

            return new Response(JSON.stringify({
                success: true,
                folder,
                marker: markerId,
            }), {
                status: 201,
                headers: { 'Content-Type': 'application/json', ...corsHeaders },
            });
        } catch (error) {
            console.error('Error creating folder:', error);
            return new Response(JSON.stringify({
                success: false,
                message: error.message || 'Failed to create folder.',
            }), {
                status: 500,
                headers: { 'Content-Type': 'application/json', ...corsHeaders },
            });
        }
    }

    // DELETE: 删除空目录占位符
    if (request.method === 'DELETE') {
        try {
            await db.delete(markerId);

            if (env.img_r2) {
                await env.img_r2.delete(markerId);
            }

            waitUntil(removeFileFromIndex(context, markerId));

            await purgeRandomFileListCache(url.origin, folder);
            await purgePublicFileListCache(url.origin, folder);

            return new Response(JSON.stringify({
                success: true,
                folder,
            }), {
                status: 200,
                headers: { 'Content-Type': 'application/json', ...corsHeaders },
            });
        } catch (error) {
            console.error('Error deleting folder marker:', error);
            return new Response(JSON.stringify({
                success: false,
                message: error.message || 'Failed to delete folder marker.',
            }), {
                status: 500,
                headers: { 'Content-Type': 'application/json', ...corsHeaders },
            });
        }
    }

    return new Response(JSON.stringify({
        success: false,
        message: 'Method not allowed. Use POST or DELETE.',
    }), {
        status: 405,
        headers: { 'Content-Type': 'application/json', ...corsHeaders },
    });
}
