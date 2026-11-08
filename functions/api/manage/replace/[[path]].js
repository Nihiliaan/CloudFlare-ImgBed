import { createS3Client } from '../../../utils/storage/s3Client.js';
import { PutObjectCommand } from "@aws-sdk/client-s3";
import { purgeCFCache, purgeRandomFileListCache, purgePublicFileListCache } from "../../../utils/purgeCache.js";
import { addFileToIndex } from "../../../utils/indexManager.js";
import { getDatabase } from '../../../utils/databaseAdapter.js';
import { getImageDimensions, purgeCDNCache } from "../../../upload/uploadTools.js";
import { WebDAVAPI } from "../../../utils/storage/webdavAPI.js";
import {
    resolveS3Credentials,
    resolveWebDAVCredentials,
} from "../../../utils/metadata/channelCredentials.js";
import { cleanPersistedMetadataInPlace } from "../../../utils/metadata/metadataSecurity.js";
import { buildFileMetadataForManagement } from "../../../utils/metadata/metadataView.js";

// CORS 跨域响应头
const corsHeaders = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, PUT, OPTIONS',
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

    // 仅允许 POST 或 PUT 方法
    if (request.method !== 'POST' && request.method !== 'PUT') {
        return new Response(JSON.stringify({
            success: false,
            message: 'Method not allowed. Use POST or PUT.',
        }), {
            status: 405,
            headers: { 'Content-Type': 'application/json', ...corsHeaders },
        });
    }

    try {
        // 从 params.path 解析 fileId
        const fileId = decodeURIComponent(params.path).split(',').join('/');
        if (!fileId) {
            return new Response(JSON.stringify({
                success: false,
                message: 'File ID is required.',
            }), {
                status: 400,
                headers: { 'Content-Type': 'application/json', ...corsHeaders },
            });
        }

        const db = getDatabase(env);
        const fileRecord = await db.getWithMetadata(fileId);

        if (!fileRecord || !fileRecord.metadata) {
            return new Response(JSON.stringify({
                success: false,
                message: 'File not found.',
            }), {
                status: 404,
                headers: { 'Content-Type': 'application/json', ...corsHeaders },
            });
        }

        // 解析 multipart/form-data
        let formData;
        try {
            formData = await request.formData();
        } catch (e) {
            return new Response(JSON.stringify({
                success: false,
                message: 'Invalid form data.',
            }), {
                status: 400,
                headers: { 'Content-Type': 'application/json', ...corsHeaders },
            });
        }

        const newFile = formData.get('file');
        if (!newFile || typeof newFile.arrayBuffer !== 'function') {
            return new Response(JSON.stringify({
                success: false,
                message: 'Uploaded file is required in "file" field.',
            }), {
                status: 400,
                headers: { 'Content-Type': 'application/json', ...corsHeaders },
            });
        }

        const buffer = await newFile.arrayBuffer();
        const newMimeType = newFile.type || fileRecord.metadata.FileType || 'application/octet-stream';
        const dimensions = getImageDimensions(buffer, newMimeType);

        const metadata = { ...fileRecord.metadata };
        metadata.FileSizeBytes = buffer.byteLength;
        metadata.FileType = newMimeType;
        metadata.TimeStamp = Date.now();
        metadata.UpdatedAt = Date.now();
        if (dimensions) {
            metadata.Width = dimensions.width;
            metadata.Height = dimensions.height;
        }

        // 按存储渠道原地覆盖物理文件
        if (metadata.Channel === 'CloudflareR2') {
            if (!env.img_r2) {
                throw new Error('R2 storage is not configured.');
            }
            await env.img_r2.put(fileId, buffer, {
                httpMetadata: { contentType: newMimeType },
            });
        } else if (metadata.Channel === 'S3') {
            const credentials = await resolveS3Credentials(env, metadata);
            const client = createS3Client(credentials);
            const targetKey = metadata.S3FileKey || fileId;
            await client.send(new PutObjectCommand({
                Bucket: credentials.bucket,
                Key: targetKey,
                Body: new Uint8Array(buffer),
                ContentType: newMimeType,
            }));
        } else if (metadata.Channel === 'WebDAV') {
            const credentials = await resolveWebDAVCredentials(env, metadata);
            const dav = new WebDAVAPI(credentials);
            const targetPath = metadata.WebDAVFilePath || fileId;
            await dav.uploadFile(targetPath, buffer, newMimeType);
        } else {
            // 其他渠道不支持原地底层物理覆盖，报错或需要特定渠道实现
            return new Response(JSON.stringify({
                success: false,
                message: `In-place replacement is not supported for channel: ${metadata.Channel}`,
            }), {
                status: 400,
                headers: { 'Content-Type': 'application/json', ...corsHeaders },
            });
        }

        cleanPersistedMetadataInPlace(metadata);

        // 原位更新数据库记录
        await db.put(fileId, fileRecord.value, { metadata });

        // 清理边缘缓存与列表缓存
        const url = new URL(request.url);
        const cdnUrl = `https://${url.hostname}/file/${fileId}`;
        await purgeCFCache(env, cdnUrl);
        const normalizedFolder = fileId.split('/').slice(0, -1).join('/');
        await purgeRandomFileListCache(url.origin, normalizedFolder);
        await purgePublicFileListCache(url.origin, normalizedFolder);

        // 同步更新索引
        waitUntil(addFileToIndex(context, fileId, metadata));

        return new Response(JSON.stringify({
            success: true,
            fileId,
            metadata: await buildFileMetadataForManagement(db, env, metadata),
        }), {
            status: 200,
            headers: { 'Content-Type': 'application/json', ...corsHeaders },
        });

    } catch (error) {
        console.error('Error replacing file:', error);
        return new Response(JSON.stringify({
            success: false,
            message: error.message || 'Internal server error.',
        }), {
            status: 500,
            headers: { 'Content-Type': 'application/json', ...corsHeaders },
        });
    }
}
