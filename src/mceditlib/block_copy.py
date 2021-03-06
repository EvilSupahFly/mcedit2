"""
   block_copy.py

   Optimized functions for copying all world data (blocks, tile entities, entities, biomes) from
   one world or schematic file to another.
"""
import time
import logging
from mceditlib import relight
from mceditlib.selection import BoundingBox, SectionBox

log = logging.getLogger(__name__)

import numpy
import mceditlib.blocktypes as blocktypes

def sourceMaskFunc(blocksToCopy):
    if blocksToCopy is not None:
        typemask = numpy.zeros(blocktypes.id_limit, dtype='bool')
        typemask[blocksToCopy] = 1

        def maskedSourceMask(sourceBlocks):
            return typemask[sourceBlocks]

        return maskedSourceMask

    def unmaskedSourceMask(_sourceBlocks):
        return numpy.ones((1,), bool)

    return unmaskedSourceMask


def copyBlocksIter(destDim, sourceDim, sourceSelection, destinationPoint, blocksToCopy=None, entities=True, create=False, biomes=False, updateLights=False):
    """
    Copy blocks and entities from the `sourceBox` area of `sourceDim` to `destDim` starting at `destinationPoint`.

    :param sourceDim: WorldEditorDimension
    :param destDim: WorldEditorDimension

    Optional parameters:
      - `blocksToCopy`: list of blockIDs to copy.
      - `entities`: True to copy Entities and TileEntities, False otherwise.
      - `create`: True to create new chunks in destLevel, False otherwise.
      - `biomes`: True to copy biome data, False otherwise.
    """

    (lx, ly, lz) = sourceSelection.size

    # needs work xxx
    log.info(u"Copying {0} blocks from {1} to {2}" .format(ly * lz * lx, sourceSelection, destinationPoint))
    startTime = time.time()

    destBox = BoundingBox(destinationPoint, sourceSelection.size)
    chunkCount = destBox.chunkCount
    i = 0
    entitiesCopied = 0
    tileEntitiesCopied = 0
    entitiesSeen = 0
    tileEntitiesSeen = 0

    if updateLights:
        allChangedX = []
        allChangedY = []
        allChangedZ = []

    makeSourceMask = sourceMaskFunc(blocksToCopy)

    copyOffset = destBox.origin - sourceSelection.origin

    # Visit each chunk in the source area
    #   Visit each section in this chunk
    #      Find the chunks and sections of the destination area corresponding to this section
    #          Compute slices for Blocks array and mask
    #          Use slices and mask to copy Blocks and Data
    #   Copy entities and tile entities from this chunk.
    sourceBiomeMask = None
    convertBlocks = blocktypes.blocktypeConverter(destDim.blocktypes, sourceDim.blocktypes)

    for sourceCpos in sourceSelection.chunkPositions():
        # Visit each chunk
        if not sourceDim.containsChunk(*sourceCpos):
            continue

        sourceChunk = sourceDim.getChunk(*sourceCpos)

        i += 1
        yield (i, chunkCount)
        if i % 20 == 0:
            log.info("Copying: Chunk {0}/{1}...".format(i, chunkCount))

        # Use sourceBiomeMask to accumulate a list of columns over all sections whose biomes should be copied.
        sourceBiomes = None
        if biomes and hasattr(sourceChunk, 'Biomes'):
            sourceBiomes = sourceChunk.Biomes
            sourceBiomeMask = numpy.zeros_like(sourceBiomes)

        for sourceCy in sourceChunk.sectionPositions():
            # Visit each section
            sourceSection = sourceChunk.getSection(sourceCy)
            if sourceSection is None:
                continue

            selectionMask = sourceSelection.section_mask(sourceCpos[0], sourceCy, sourceCpos[1])
            if selectionMask is None:
                continue

            typeMask = makeSourceMask(sourceSection.Blocks)
            sourceMask = selectionMask & typeMask

            # Update sourceBiomeMask
            if sourceBiomes is not None:
                sourceBiomeMask |= sourceMask.any(axis=0)

            # Find corresponding destination area(s)
            sectionBox = SectionBox(sourceCpos[0], sourceCy, sourceCpos[1])
            destBox = BoundingBox(sectionBox.origin + copyOffset, sectionBox.size)

            for destCpos in destBox.chunkPositions():
                if not create and not destDim.containsChunk(*destCpos):
                    continue
                destChunk = destDim.getChunk(*destCpos, create=True)

                for destCy in destBox.sectionPositions(*destCpos):
                    # Compute slices for source and dest arrays
                    destSectionBox = SectionBox(destCpos[0], destCy, destCpos[1])
                    intersect = destSectionBox.intersect(destBox)
                    if intersect.volume == 0:
                        continue

                    destSection = destChunk.getSection(destCy, create=True)
                    if destSection is None:
                        continue

                    destSlices = (
                        slice(intersect.miny - (destCy << 4), intersect.maxy - (destCy << 4)),
                        slice(intersect.minz - (destCpos[1] << 4), intersect.maxz - (destCpos[1] << 4)),
                        slice(intersect.minx - (destCpos[0] << 4), intersect.maxx - (destCpos[0] << 4)),
                    )

                    sourceIntersect = BoundingBox(intersect.origin - copyOffset, intersect.size)
                    sourceSlices = (
                        slice(sourceIntersect.miny - (sourceCy << 4), sourceIntersect.maxy - (sourceCy << 4)),
                        slice(sourceIntersect.minz - (sourceCpos[1] << 4), sourceIntersect.maxz - (sourceCpos[1] << 4)),
                        slice(sourceIntersect.minx - (sourceCpos[0] << 4), sourceIntersect.maxx - (sourceCpos[0] << 4)),
                    )
                    # Read blocks
                    sourceBlocks = sourceSection.Blocks[sourceSlices]
                    sourceData = sourceSection.Data[sourceSlices]
                    sourceMaskPart = sourceMask[sourceSlices]

                    # Convert blocks
                    convertedSourceBlocks, convertedSourceData = convertBlocks(sourceBlocks, sourceData)
                    convertedSourceBlocksMasked = convertedSourceBlocks[sourceMaskPart]

                    # Find blocks that need direct lighting update - block opacity or brightness changed

                    oldBrightness = destDim.blocktypes.brightness[destSection.Blocks[destSlices][sourceMaskPart]]
                    newBrightness = destDim.blocktypes.brightness[convertedSourceBlocksMasked]
                    oldOpacity = destDim.blocktypes.opacity[destSection.Blocks[destSlices][sourceMaskPart]]
                    newOpacity = destDim.blocktypes.opacity[convertedSourceBlocksMasked]
                    changedLight = (oldBrightness != newBrightness) | (oldOpacity != newOpacity)

                    # Write blocks
                    destSection.Blocks[destSlices][sourceMaskPart] = convertedSourceBlocks[sourceMaskPart]
                    destSection.Data[destSlices][sourceMaskPart] = convertedSourceData[sourceMaskPart]

                    if updateLights:
                        # Find coordinates of lighting updates
                        (changedFlat,) = changedLight.nonzero()
                        # Since convertedSourceBlocksMasked is a 1d array, changedFlat is an index
                        # into this array. Thus, changedFlat is also an index into the nonzero values
                        # of sourceMaskPart.

                        if len(changedFlat):
                            x, y, z = sourceMaskPart.nonzero()
                            changedX = x[changedFlat].astype('i4')
                            changedY = y[changedFlat].astype('i4')
                            changedZ = z[changedFlat].astype('i4')

                            changedX += intersect.minx
                            changedY += intersect.miny
                            changedZ += intersect.minz
                            if updateLights == "all":
                                allChangedX.append(changedX)
                                allChangedY.append(changedY)
                                allChangedZ.append(changedZ)
                            else:
                                # log.info("Updating section lights in %s blocks... (ob %s)",
                                #          changedFlat.shape,
                                #          oldBrightness.shape)
                                relight.updateLightsByCoord(destDim, changedX, changedY, changedZ)

                destChunk.dirty = True

        # Copy biomes
        if sourceBiomes is not None:
            bx, bz = sourceBiomeMask.nonzero()
            wbx = bx + (sourceCpos[0] << 4)
            wbz = bz + (sourceCpos[1] << 4)
            destDim.setBlocks(wbx, 1, wbz, Biomes=sourceBiomes[bx, bz])

        # Copy entities and tile entities
        if entities:
            entitiesSeen += len(sourceChunk.Entities)
            for entity in sourceChunk.Entities:
                if entity.Position in sourceSelection:
                    entitiesCopied += 1
                    newEntity = entity.copyWithOffset(copyOffset)
                    destDim.addEntity(newEntity)

        tileEntitiesSeen += len(sourceChunk.TileEntities)
        for tileEntity in sourceChunk.TileEntities:
            if tileEntity.Position in sourceSelection:
                tileEntitiesCopied += 1
                newEntity = tileEntity.copyWithOffset(copyOffset)
                destDim.addTileEntity(newEntity)

    duration = time.time() - startTime
    log.info("Duration: %0.3fs, %d/%d chunks, %0.2fms per chunk (%0.2f chunks per second)",
             duration, i, sourceSelection.chunkCount, 1000 * duration/i, i/duration)
    log.info("Copied %d/%d entities and %d/%d tile entities",
             entitiesCopied, entitiesSeen, tileEntitiesCopied, tileEntitiesSeen)

    if updateLights == "all":
        log.info("Updating all at once for %d sections (%d cells)", len(allChangedX), sum(len(a) for a in allChangedX))

        startTime = time.time()

        for i in range(len(allChangedX)):
            x = allChangedX[i]
            y = allChangedY[i]
            z = allChangedZ[i]
            relight.updateLightsByCoord(destDim, x, y, z)

        i = i or 1
        duration = time.time() - startTime
        duration = duration or 1

        log.info("Lighting complete.")
        log.info("Duration: %0.3fs, %d sections, %0.2fms per section (%0.2f sections per second)",
                 duration, i, 1000 * duration/i, i/duration)


