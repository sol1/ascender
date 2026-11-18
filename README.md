# Screenshots for ctrliq/ascender#1215

Images referenced from the feature request for a native NetBox inventory
source. They live on a branch of their own so they stay out of the history of
`netbox-deep-integration`, which is the branch actually proposed for review.

Every one of them was taken against the development environment on that
branch:

    make docker-compose NETBOX=true
    tools/docker-compose/netbox-seed.py

Nothing here is real infrastructure. The hosts, addresses and domains all come
from the seed script.
