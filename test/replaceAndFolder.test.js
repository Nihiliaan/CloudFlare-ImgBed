import assert from 'node:assert/strict';
import { loadFunction } from './helpers/loadFunction.js';
import { createContext } from './helpers/context.js';
import { managementFixture } from './helpers/management.js';

describe('image replacement and folder management (Issue #763 & #456)', () => {
    let fixture, replace, folder, list;

    beforeEach(async () => {
        fixture = await managementFixture();
        replace = await loadFunction('functions/api/manage/replace/[[path]].js', fixture.mocks);
        folder = await loadFunction('functions/api/manage/folder/[[path]].js', fixture.mocks);
        list = await loadFunction('functions/api/manage/list.js', fixture.mocks);
    });

    it('in-place replaces file content and metadata while keeping fileId and URL unchanged', async () => {
        // Seed an existing file
        const originalMetadata = {
            Channel: 'CloudflareR2', FileName: 'avatar.png', FileType: 'image/png',
            FileSizeBytes: 100, TimeStamp: 1000, Directory: 'users/'
        };
        await fixture.seed([{ id: 'users/avatar.png', metadata: originalMetadata }]);

        // Construct multipart form data with replacement file
        const newFileBlob = new Blob(['new-replacement-binary-data'], { type: 'image/png' });
        const formData = new FormData();
        formData.append('file', newFileBlob, 'new_avatar.png');

        const replaceCtx = createContext('https://test/api/manage/replace/users,avatar.png', {
            method: 'POST', env: fixture.env, params: { path: 'users,avatar.png' },
            headers: { 'Content-Type': 'multipart/form-data' }
        });
        // Override request.formData to return our simulated form data
        replaceCtx.request.formData = async () => formData;

        const response = await replace.onRequest(replaceCtx);
        assert.equal(response.status, 200);

        const result = await response.json();
        assert.equal(result.success, true);
        assert.equal(result.fileId, 'users/avatar.png');

        // Check updated metadata in DB
        const updatedRecord = await fixture.db.getWithMetadata('users/avatar.png');
        assert.equal(updatedRecord.metadata.FileSizeBytes, 27); // 'new-replacement-binary-data'.length
        assert.equal(updatedRecord.metadata.Directory, 'users/');
        assert.equal(updatedRecord.metadata.FileName, 'avatar.png');
        assert.ok(updatedRecord.metadata.TimeStamp >= 1000);

        // Check updated binary object in R2
        assert.equal(fixture.objects.get('users/avatar.png'), 'new-replacement-binary-data');

        // Check cache invalidation was triggered for the exact URL
        assert.ok(fixture.purges.some(([type, url]) => type === 'file' && url.includes('users/avatar.png')));
    });

    it('creates persistent empty folder and ensures it appears in directory listings', async () => {
        // 1. Initially list root directories (should be empty)
        const listCtx1 = createContext('https://test/api/manage/list?dir=', { env: fixture.env });
        const listRes1 = await list.onRequest(listCtx1);
        const listData1 = await listRes1.json();
        assert.deepEqual(listData1.directories, []);

        // 2. Create folder "my_folder/sub_album"
        const folderCtx = createContext('https://test/api/manage/folder', {
            method: 'POST', env: fixture.env, body: JSON.stringify({ folder: 'my_folder/sub_album' })
        });
        folderCtx.request.json = async () => ({ folder: 'my_folder/sub_album' });
        const folderRes = await folder.onRequest(folderCtx);
        assert.equal(folderRes.status, 201);

        // Wait for background index update
        await Promise.all(folderCtx.backgroundTasks);

        // 3. List root directory (should now contain "my_folder")
        const listCtx2 = createContext('https://test/api/manage/list?dir=', { env: fixture.env });
        const listRes2 = await list.onRequest(listCtx2);
        const listData2 = await listRes2.json();
        assert.ok(listData2.directories.includes('my_folder'));
        // And .keep should NOT appear in files list!
        assert.equal(listData2.files.some(f => f.name.endsWith('.keep')), false);

        // 4. List "my_folder" directory (should contain "my_folder/sub_album")
        const listCtx3 = createContext('https://test/api/manage/list?dir=my_folder', { env: fixture.env });
        const listRes3 = await list.onRequest(listCtx3);
        const listData3 = await listRes3.json();
        assert.ok(listData3.directories.includes('my_folder/sub_album'));
        assert.equal(listData3.files.length, 0); // No visible files, only directory!
    });
});
