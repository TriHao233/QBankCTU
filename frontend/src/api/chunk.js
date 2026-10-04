import { apiRequest } from '../services/apiClient';

export function chunkDocument(documentId, collectionName) {
  const payload = { document_id: documentId };
  if (collectionName) {
    payload.collection_name = collectionName;
  }
  return apiRequest('/chunk/document', {
    method: 'POST',
    body: payload,
  });
}
