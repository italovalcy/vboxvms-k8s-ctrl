import asyncio
import pprint
import time
import random

import kopf
import yaml


TASKS = {}

@kopf.on.startup()
async def startup_fn_simple(logger, **kwargs):
    logger.info('Starting in 1s...')
    await asyncio.sleep(1)


@kopf.on.cleanup()
async def cleanup_fn(logger, **kwargs):
    logger.info('Cleaning up in 3s...')
    await asyncio.sleep(3)


@kopf.on.create('vboxvms', "v1", "amlight.net")
async def create(body, meta, spec, status, logger, name, namespace, **kwargs):
    logger.info("Create body: %s" % (body))
    kopf.info(body, reason='AnyReason')
    kopf.event(body, type='Warning', reason='SomeReason', message="Cannot do something")
    status['phase'] = 'Pending'
    TASKS[f"{name}-{namespace}"] = asyncio.create_task(check_status(spec, status, logger))
    return {'job1-status': 100}


@kopf.on.delete('vboxvms', "v1", "amlight.net")
async def delete(body, logger, **kwargs):
    logger.info("Delete body: %s" % (body))


async def check_status(spec, status, logger):
    while True:
        await asyncio.sleep(3)
        logger.info("checking status again...")
        if random.randint(1, 10) == 7:
            status["phase"] = "Running"
            break
