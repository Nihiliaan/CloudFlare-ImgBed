import assert from 'node:assert/strict';
import { loadFunction } from './helpers/loadFunction.js';
import { createContext } from './helpers/context.js';
import { managementFixture } from './helpers/management.js';
import * as fileTools from '../functions/file/fileTools.js';
import * as imageTransform from '../functions/file/imageTransform.js';
import * as fileExpiryHelpers from '../functions/utils/fileExpiryHelpers.js';

describe('short URL and move redirect (Issue #763)', () => {
    let fixture, route, move;

    beforeEach(async () => {
        fixture = await managementFixture();
        // Ensure mock img_r2.get implements writeHttpMetadata
        fixture.env.img_r2.get = async (id) => {
            return fixture.objects.has(id) ? {
                body: new Response(fixture.objects.get(id)).body,
                writeHttpMetadata(headers) { headers.set('Content-Type', 'image/png'); }
            } : null;
        };

        route = await loadFunction('functions/file/[[path]].js', {
            ...fixture.mocks, ...fileTools, ...imageTransform, ...fileExpiryHelpers,
            fetchSecurityConfig: async () => ({ access: { whiteListMode: false } }),
        });
        move = await loadFunction('functions/api/manage/move/[[path]].js', fixture.mocks);
    });

    it('resolves historical path via redirect pointer after file is moved (HTTP 200 transparent serve)', async () => {
        // Seed a file in album/pic.png
        const metadata = { Channel: 'CloudflareR2', FileName: 'pic.png', FileType: 'image/png', Directory: 'album/' };
        await fixture.seed([{ id: 'album/pic.png', metadata }]);

        // Move the file from album/pic.png to archive/pic.png
        const moveCtx = createContext('https://test/api/manage/move/album,pic.png?dist=archive', {
            method: 'POST', env: fixture.env, params: { path: 'album,pic.png' }
        });
        const moveRes = await move.onRequest(moveCtx);
        assert.equal(moveRes.status, 200);

        // Verify old key is deleted from main records but has redirect pointer
        assert.equal(await fixture.db.getWithMetadata('album/pic.png'), null);
        assert.equal(await fixture.db.get('manage@redirect@album/pic.png'), 'archive/pic.png');

        // Requesting the OLD URL should transparently serve the file from archive/pic.png
        const getOldCtx = createContext('https://test/file/album/pic.png', {
            params: { path: 'album,pic.png' }, env: fixture.env
        });
        const oldRes = await route.onRequest(getOldCtx);
        assert.equal(oldRes.status, 200);
        assert.equal(await oldRes.text(), 'file-content');

        // Requesting with redirect=true should return 302
        const getRedirectCtx = createContext('https://test/file/album/pic.png?redirect=true', {
            params: { path: 'album,pic.png' }, env: fixture.env
        });
        const redirectRes = await route.onRequest(getRedirectCtx);
        assert.equal(redirectRes.status, 302);
        assert.equal(redirectRes.headers.get('Location'), 'https://test/file/archive/pic.png');
    });

    it('resolves file via short base name without folder path', async () => {
        // Seed a file in nested/folder/image123.png
        const metadata = { Channel: 'CloudflareR2', FileName: 'image123.png', FileType: 'image/png', Directory: 'nested/folder/' };
        await fixture.seed([{ id: 'nested/folder/image123.png', metadata }]);

        // Set alias directly
        await fixture.db.put('manage@alias@image123.png', 'nested/folder/image123.png');

        // Access via root short URL /file/image123.png
        const ctx = createContext('https://test/file/image123.png', {
            params: { path: 'image123.png' }, env: fixture.env
        });
        const res = await route.onRequest(ctx);
        assert.equal(res.status, 200);
        assert.equal(await res.text(), 'file-content');
    });

    it('falls back to index scan when alias is not cached, then populates alias', async () => {
        // Seed a file in secret/vault/doc.png (no alias in db)
        const metadata = { Channel: 'CloudflareR2', FileName: 'doc.png', FileType: 'image/png', Directory: 'secret/vault/' };
        await fixture.seed([{ id: 'secret/vault/doc.png', metadata }]);

        // Verify alias doesn't exist yet
        assert.equal(await fixture.db.get('manage@alias@doc.png'), null);

        // Access via root short URL /file/doc.png
        const ctx = createContext('https://test/file/doc.png', {
            params: { path: 'doc.png' }, env: fixture.env
        });
        const res = await route.onRequest(ctx);
        assert.equal(res.status, 200);
        assert.equal(await res.text(), 'file-content');

        // Verify alias was automatically cached
        assert.equal(await fixture.db.get('manage@alias@doc.png'), 'secret/vault/doc.png');
    });
});
