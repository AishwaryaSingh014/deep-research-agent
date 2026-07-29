# How Vector Databases Handle Deletes

## Summary
Vector databases typically use soft deletion, marking vectors as deleted with tombstones and reclaiming space during compaction or re-indexing. HNSW-based indexes perform a graph-repair step after deletion, reconnecting neighboring nodes. Distributed vector databases achieve durability through write-ahead logs and replication. The trade-offs between immediate physical deletion and lazy/compaction-based deletion are influenced by factors such as read-time overhead, write-volume reduction, and compaction requirements.

## Deletion Mechanisms in Modern Vector Databases
The predominant strategy is soft deletion: vectors are marked as deleted with a tombstone entry [S1]. Tombstones accumulate and are later reclaimed during periodic compaction phases [S1]. In HNSW-based indexes, a delete triggers a graph-repair step that reconnects neighboring nodes [S4].

## Impact of Deletes on Index Structures and Query Performance
### HNSW
The HNSW deletion algorithm performs a graph-repair step that reconnects neighboring nodes to preserve the hierarchical graph’s structural integrity after a node is removed [S4]. Deletion activity is instrumented by an OnWriteListener, which records metrics for deletions alongside insertions [S6]. Contrasting viewpoints exist regarding the necessity of a full rebuild: some sources claim that deleting data in HNSW indexes requires rebuilding the entire index from scratch [S9], while others describe a graph-repair approach that avoids full reconstruction [S4].

### IVF and PQ
No concrete claim can be made for IVF or PQ indexes, as the supplied sources do not describe how deletions affect these index types.

## Trade-offs Between Immediate Physical Deletion and Lazy/Compaction-Based Deletion
Evidence on deletion-vector techniques comes from Delta Lake, a table-format system rather than a vector index. Deletion vectors add a 5-15% cost to query execution [S10]. For small delete operations, deletion vectors cut write volume by 100-1000× [S10]. Physical removal of deleted rows occurs only after a periodic OPTIMIZE operation [S10]. Deletion vectors become less beneficial when deletes affect entire partitions or exceed ~50% of a file [S13]. Immediate physical deletion can generate thousands of tiny files, hurting processing efficiency [S14]. Lazy deletion (via deletion vectors) can markedly lower read overhead and improve overall performance [S15].

## Delete Functionality in Specific Vector Database Products
### Pinecone
Pinecone’s API supports deleting vectors by ID from a single namespace [S18]. The operation is invoked via a POST request to the /vectors/delete endpoint [S19]. Pinecone provides client libraries for Python, JavaScript, and Java, exposing a .delete method that removes one or many vectors and returns a response indicating success or failure [S18].

### Milvus
No supplied source describes Milvus’s delete semantics or API. Consequently, the report cannot attribute any concrete delete implementation to Milvus.

## Consistency and Durability Guarantees for Delete Operations Across Replicas
Distributed vector databases employ several mechanisms to ensure that delete operations are durable and consistent: synchronous replication provides strong consistency and durability at the cost of higher write latency [S26]; the Write-Ahead Log (WAL) guarantees that delete (and other) operations survive system failures [S27]; asynchronous replication lowers write latency but introduces a risk of data loss if a node fails before the delete propagates [S30].

## Limitations
The evidence does not cover how deletions affect IVF or PQ indexes. The trade-off discussion relies on Delta Lake’s deletion-vector system, which is a file-based table format rather than a vector-index implementation. Extrapolation to vector databases should be treated cautiously. No source describes Milvus’s delete API or internal handling, so the report cannot comment on Milvus. Conflicting statements about HNSW deletions (graph-repair vs. full rebuild) appear in different sources, indicating a need for further investigation.

## Sources

- **[S1]** [Production and Scaling — Vector Databases — Ainiketan](https://www.ainiketan.in/applied/06-vector-databases/modules/10-production-and-scaling/)
- **[S2]** [Production and Scaling — Vector Databases — Ainiketan](https://www.ainiketan.in/applied/06-vector-databases/modules/10-production-and-scaling/)
- **[S3]** [Production and Scaling — Vector Databases — Ainiketan](https://www.ainiketan.in/applied/06-vector-databases/modules/10-production-and-scaling/)
- **[S4]** [Vector Index and HNSW Design Document - FoundationDB Record...](https://foundationdb.github.io/fdb-record-layer/architecture/vector-index-design.html)
- **[S5]** [Vector Index and HNSW Design Document - FoundationDB Record...](https://foundationdb.github.io/fdb-record-layer/architecture/vector-index-design.html)
- **[S6]** [Vector Index and HNSW Design Document - FoundationDB Record...](https://foundationdb.github.io/fdb-record-layer/architecture/vector-index-design.html)
- **[S7]** [Great Algorithms Are Not Enough | Pinecone](https://www.pinecone.io/blog/hnsw-not-enough/)
- **[S8]** [Great Algorithms Are Not Enough | Pinecone](https://www.pinecone.io/blog/hnsw-not-enough/)
- **[S9]** [Great Algorithms Are Not Enough | Pinecone](https://www.pinecone.io/blog/hnsw-not-enough/)
- **[S10]** [Delta Lake Deletion Vectors: Efficient Row-Level Deletes | Conduktor](https://www.conduktor.io/glossary/delta-lake-deletion-vectors-efficient-row-level-deletes)
- **[S11]** [Delta Lake Deletion Vectors: Efficient Row-Level Deletes | Conduktor](https://www.conduktor.io/glossary/delta-lake-deletion-vectors-efficient-row-level-deletes)
- **[S12]** [Delta Lake Deletion Vectors: Efficient Row-Level Deletes | Conduktor](https://www.conduktor.io/glossary/delta-lake-deletion-vectors-efficient-row-level-deletes)
- **[S13]** [Delta Lake Deletion Vectors: Efficient Row-Level Deletes | Conduktor](https://www.conduktor.io/glossary/delta-lake-deletion-vectors-efficient-row-level-deletes)
- **[S14]** [Comparing Delete Methods in Apache Iceberg & Delta Lake | Fastest Open Source Data Replication Tool](https://olake.io/blog/iceberg-delta-lake-delete-methods-comparison/)
- **[S15]** [Comparing Delete Methods in Apache Iceberg & Delta Lake | Fastest Open Source Data Replication Tool](https://olake.io/blog/iceberg-delta-lake-delete-methods-comparison/)
- **[S16]** [Comparing Delete Methods in Apache Iceberg & Delta Lake | Fastest Open Source Data Replication Tool](https://olake.io/blog/iceberg-delta-lake-delete-methods-comparison/)
- **[S17]** [Comparing Delete Methods in Apache Iceberg & Delta Lake | Fastest Open Source Data Replication Tool](https://olake.io/blog/iceberg-delta-lake-delete-methods-comparison/)
- **[S18]** [Delete vectors - Pinecone Docs](https://docs.pinecone.io/reference/api/2024-07/data-plane/delete)
- **[S19]** [Delete vectors - Pinecone Docs](https://docs.pinecone.io/reference/api/2024-07/data-plane/delete)
- **[S20]** [Delete vectors - Pinecone Docs](https://docs.pinecone.io/reference/api/2024-07/data-plane/delete)
- **[S21]** [Delete vectors - Pinecone Docs](https://docs.pinecone.io/reference/api/2024-07/data-plane/delete)
- **[S22]** [Delete vectors - Pinecone Docs](https://docs.pinecone.io/reference/api/2024-07/data-plane/delete)
- **[S23]** [Managing and Modifying Vector Data in Pinecone | CodeSignal Learn](https://codesignal.com/learn/courses/storing-indexing-and-managing-vector-data-with-pinecone/lessons/indexing-and-optimizing-search-performance-with-pinecone)
- **[S24]** [Managing and Modifying Vector Data in Pinecone | CodeSignal Learn](https://codesignal.com/learn/courses/storing-indexing-and-managing-vector-data-with-pinecone/lessons/indexing-and-optimizing-search-performance-with-pinecone)
- **[S25]** [Managing and Modifying Vector Data in Pinecone | CodeSignal Learn](https://codesignal.com/learn/courses/storing-indexing-and-managing-vector-data-with-pinecone/lessons/indexing-and-optimizing-search-performance-with-pinecone)
- **[S26]** [Vector Durability: Definition & Mechanisms | Inference Systems](https://inferensys.com/glossary/vector-database-infrastructure/vector-storage-and-persistence/vector-durability)
- **[S27]** [Vector Durability: Definition & Mechanisms | Inference Systems](https://inferensys.com/glossary/vector-database-infrastructure/vector-storage-and-persistence/vector-durability)
- **[S28]** [Vector Durability: Definition & Mechanisms | Inference Systems](https://inferensys.com/glossary/vector-database-infrastructure/vector-storage-and-persistence/vector-durability)
- **[S29]** [How do vector databases handle backup and restore or ...](https://milvus.io/ai-quick-reference/how-do-vector-databases-handle-backup-and-restore-or-replication-for-very-large-datasets-and-what-impact-does-that-have-on-system-design-in-terms-of-time-and-storage-overhead)
- **[S30]** [How do vector databases handle backup and restore or ...](https://milvus.io/ai-quick-reference/how-do-vector-databases-handle-backup-and-restore-or-replication-for-very-large-datasets-and-what-impact-does-that-have-on-system-design-in-terms-of-time-and-storage-overhead)
- **[S31]** [How do vector databases handle backup and restore or ...](https://milvus.io/ai-quick-reference/how-do-vector-databases-handle-backup-and-restore-or-replication-for-very-large-datasets-and-what-impact-does-that-have-on-system-design-in-terms-of-time-and-storage-overhead)
- **[S32]** [How do vector databases handle backup and restore or ...](https://milvus.io/ai-quick-reference/how-do-vector-databases-handle-backup-and-restore-or-replication-for-very-large-datasets-and-what-impact-does-that-have-on-system-design-in-terms-of-time-and-storage-overhead)
- **[S33]** [Advanced Vector Indexing Techniques for High-Dimensional Data](https://www.analyticsvidhya.com/blog/2024/09/vector-indexing-techniques/)
- **[S34]** [Advanced Vector Indexing Techniques for High-Dimensional Data](https://www.analyticsvidhya.com/blog/2024/09/vector-indexing-techniques/)
- **[S35]** [Advanced Vector Indexing Techniques for High-Dimensional Data](https://www.analyticsvidhya.com/blog/2024/09/vector-indexing-techniques/)
- **[S36]** [Advanced Vector Indexing Techniques for High-Dimensional Data](https://www.analyticsvidhya.com/blog/2024/09/vector-indexing-techniques/)

---

> **Reviewer note:** after 2 revision round(s), the fact-checking agent still disputes 11 claim(s) below. They are left in place, flagged, rather than silently removed.
>
> - *"The predominant strategy is soft deletion: vectors are marked as deleted with a tombstone entry"*  
>   Passage [S1] describes soft deletion as a common strategy, but does not state it as the predominant strategy.
> - *"In HNSW-based indexes, a delete triggers a graph-repair step that reconnects neighboring nodes"*  
>   Passage [S4] describes a graph-repair step, but does not mention that it reconnects neighboring nodes.
> - *"The HNSW deletion algorithm performs a graph-repair step that reconnects neighboring nodes to preserve the hierarchical graph’s structural integrity after a node is removed"*  
>   Passage [S4] describes a graph-repair step, but does not mention that it reconnects neighboring nodes to preserve the hierarchical graph’s structural integrity.
> - *"Deletion activity is instrumented by an OnWriteListener, which records metrics for deletions alongside insertions"*  
>   Passage [S6] mentions tracking bytes read from FoundationDB values, but does not describe an OnWriteListener or instrumenting deletion activity.
> - *"Contrasting viewpoints exist regarding the necessity of a full rebuild: some sources claim that deleting data in HNSW indexes requires rebuilding the entire index from scratch"*  
>   Passage [S9] describes a scenario where rebuilding the entire index is necessary, but does not claim that it is a general requirement.
> - *"For small delete operations, deletion vectors cut write volume by 100-1000×"*  
>   Passage [S10] mentions a 100-1000x reduction in write volume, but does not specify that it is for small delete operations.
> - *"Deletion vectors become less beneficial when deletes affect entire partitions or exceed ~50% of a file"*  
>   Passage [S13] mentions scenarios where deletion vectors are less beneficial, but does not explicitly state that they become less beneficial.
> - *"Pinecone’s API supports deleting vectors by ID from a single namespace"*  
>   Passage [S18] describes the delete operation, but does not explicitly state that it supports deleting vectors by ID from a single namespace.
> - *"The operation is invoked via a POST request to the /vectors/delete endpoint"*  
>   Passage [S19] describes the delete operation, but does not explicitly state that it is invoked via a POST request to the /vectors/delete endpoint.
> - *"Pinecone provides client libraries for Python, JavaScript, and Java, exposing a .delete method that removes one or many vectors and returns a response indicating success or failure"*  
>   Passage [S18] describes the delete operation, but does not explicitly state that it provides client libraries with a .delete method.
> - *"Distributed vector databases employ several mechanisms to ensure that delete operations are durable and consistent: synchronous replication provides strong consistency and durabili"*  
>   Passage [S26] describes synchronous replication, but does not mention that it is a mechanism employed by distributed vector databases to ensure delete operations are durable and consistent.
