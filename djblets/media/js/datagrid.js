/*
 * Copyright 2008-2010 Christian Hammond.
 * Copyright 2010-2012 Beanbag, Inc.
 *
 * Licensed under the MIT license.
 */
(function($) {


/*
 * Creates a datagrid. This will cause drag and drop and column
 * customization to be enabled.
 */
$.fn.datagrid = function() {
    var $grid = this,
        gridId = this.attr("id"),
        $editButton = $("#" + gridId + "-edit"),
        $menu = $("#" + gridId + "-menu"),
        $summaryCells = $grid.find("td.summary");

        /* State */
        activeColumns = [],
        $activeMenu = null,
        columnMidpoints = [],
        dragColumn = null,
        dragColumnsChanged = false,
        dragColumnWidth = 0,
        dragIndex = 0,
        dragLastX = 0;

    $grid.data('datagrid', this);

    /* Add all the non-special columns to the list. */
    $grid.find("col").not(".datagrid-customize").each(function(i, col) {
        activeColumns.push(col.className);
    });

    $grid.find("th")
        /* Make the columns unselectable. */
        .disableSelection()

        /* Make the columns draggable. */
        .not(".edit-columns").draggable({
            appendTo: "body",
            axis: "x",
            containment: $grid.find("thead:first"),
            cursor: "move",
            helper: function() {
                var $el = $(this);

                return $("<div/>")
                    .addClass("datagrid-header-drag datagrid-header")
                    .width($el.width())
                    .height($el.height())
                    .css("top", $el.offset().top)
                    .html($el.html());
            },
            start: startColumnDrag,
            stop: endColumnDrag,
            drag: onColumnDrag
        });

    /* Register callbacks for the columns. */
    $menu.find("tr").each(function(i, row) {
        var className = row.className;

        $(row).find(".datagrid-menu-checkbox, .datagrid-menu-label a")
            .click(function() {
                toggleColumn(className);
            });
    });

    $editButton.click(function(evt) {
        evt.stopPropagation();
        toggleColumnsMenu();
    });

    /*
     * Attaches click event listener to all summary td elements,
     * following href of child anchors if present.  This is being
     * done to complement the "cursor:pointer" style that is
     * already applied to the same elements. (Bug #1022)
     */
    $summaryCells.click(function(evt) {
        var cellHref = $(evt.target).find("a").attr("href");

        evt.stopPropagation();

        if (cellHref){
            window.location.href = cellHref;
        }
    });

    $(document.body).click(hideColumnsMenu);


    /********************************************************************
     * Public methods
     ********************************************************************/
    this.reload = function() {
        loadFromServer(null, true);
    };


    /********************************************************************
     * Server communication
     ********************************************************************/

    function loadFromServer(params, reloadGrid) {
        var search = window.location.search || '?',
            url = window.location.pathname + search +
                  '&gridonly=1&datagrid-id=' + gridId;

        if (params) {
            url += '&' + params;
        }

        $.get(url, function(html) {
            if (reloadGrid) {
                $grid.replaceWith(html);
                $("#" + gridId).datagrid();
            }
        });
    };


    /********************************************************************
     * Column customization
     ********************************************************************/

    /*
     * Hides the currently open columns menu.
     */
    function hideColumnsMenu() {
        if ($activeMenu !== null) {
            $activeMenu.hide();
            $activeMenu = null;
        }
    }

    /*
     * Toggles the visibility of the specified columns menu.
     */
    function toggleColumnsMenu() {
        var offset;

        if ($menu.is(":visible")) {
            hideColumnsMenu();
        } else {
            offset = $editButton.offset()

            $menu
                .css({
                    left: offset.left - $menu.outerWidth() +
                          $editButton.outerWidth(),
                    top:  offset.top + $editButton.outerHeight()
                })
                .show();

            $activeMenu = $menu;
        }
    }

    /*
     * Saves the new columns list on the server.
     *
     * @param {string}   columnsStr  The columns to display.
     * @param {boolean}  reloadGrid  Reload from the server.
     */
    function saveColumns(columnsStr, reloadGrid) {
        loadFromServer('columns=' + columnsStr, reloadGrid);
    }

    /*
     * Toggles the visibility of a column. This will build the resulting
     * columns string and request a save of the columns, followed by a
     * reload of the page.
     *
     * @param {string}  columnId  The ID of the column to toggle.
     */
    function toggleColumn(columnId) {
        saveColumns(serializeColumns(columnId), true);
    }

    /*
     * Serializes the active column list, optionally adding one new entry
     * to the end of the list.
     *
     * @return The serialized column list.
     */
    function serializeColumns(addedColumn) {
        var columnsStr = "";

        $(activeColumns).each(function(i) {
            var curColumn = activeColumns[i];

            if (curColumn === addedColumn) {
                /* We're removing this column. */
                addedColumn = null;
            } else {
                columnsStr += curColumn;

                if (i < activeColumns.length - 1) {
                    columnsStr += ",";
                }
            }
        });

        if (addedColumn) {
            columnsStr += "," + addedColumn;
        }

        return columnsStr;
    }


    /********************************************************************
     * Column reordering support
     ********************************************************************/

    /*
     * Handles the beginning of the drag.
     *
     * Builds the column information needed for determining when we should
     * switch columns.
     *
     * @param {event}  evt The event.
     * @param {object} ui  The jQuery drag and drop information.
     */
    function startColumnDrag(evt, ui) {
        dragColumn = this;
        dragColumnsChanged = false;
        dragColumnWidth = ui.helper.width();
        dragIndex = 0;
        dragLastX = 0;
        buildColumnInfo();

        /* Hide the column but keep its area reserved. */
        $(dragColumn).css("visibility", "hidden");
    }

    /*
     * Handles the end of a drag.
     *
     * This shows the original header (now in its new place) and saves
     * the new columns configuration.
     */
    function endColumnDrag() {
        var $column = $(this);

        /* Re-show the column header. */
        $column.css("visibility", "visible");

        columnMidpoints = [];

        if (dragColumnsChanged) {
            /* Build the new columns list */
            saveColumns(serializeColumns());
        }
    }

    /*
     * Handles movement while in drag mode.
     *
     * This will check if we've crossed the midpoint of a column. If so, we
     * switch the columns.
     *
     * @param {event}  e  The event.
     * @param {object} ui The jQuery drag and drop information.
     */
    function onColumnDrag(e, ui) {
        /*
         * Check the direction we're moving and see if we're ready to switch
         * with another column.
         */
        var x = e.originalEvent.pageX,
            hitX = -1,
            index = -1;

        if (x === dragLastX) {
            /* No change that we care about. Bail out. */
            return;
        }

        if (x < dragLastX) {
            index = dragIndex - 1;
            hitX = ui.offset.left;
        } else {
            index = dragIndex + 1;
            hitX = ui.offset.left + ui.helper.width();
        }

        if (index >= 0 && index < columnMidpoints.length) {
            /* Check that we're dragging past the midpoint. If so, swap. */
            if (x < dragLastX && hitX <= columnMidpoints[index]) {
                swapColumnBefore(dragIndex, index);
            } else if (x > dragLastX && hitX >= columnMidpoints[index]) {
                swapColumnBefore(index, dragIndex);
            }
        }

        dragLastX = x;
    }

    /*
     * Builds the necessary information on the columns.
     *
     * This will construct an array of midpoints that are used to determine
     * when we should swap columns during a drag. It also sets the index
     * of the currently dragged column.
     */
    function buildColumnInfo() {
        /* Clear and rebuild the list of mid points. */
        columnMidpoints = [];

        $grid.find("th").not(".edit-columns").each(function(i, column) {
            var $column = $(column),
                offset = $column.offset();

            if (column === dragColumn) {
                dragIndex = i;

                /*
                 * Getting the width of an invisible element is very bad
                 * when the element is a <th>. Use our pre-calculated width.
                 */
                width = dragColumnWidth;
            } else {
                width = $column.width();
            }

            columnMidpoints.push(Math.round(offset.left + width / 2));
        });
    }

    /*
     * Swaps two columns, placing the first before the second.
     *
     * It is assumed that the two columns are siblings. Horrible disfiguring
     * things might happen if this isn't the case, or it might not. Who
     * can tell. Our code behaves, though.
     *
     * @param {int} index       The index of the column to move.
     * @param {int} beforeIndex The index of the column to place the first
     *                          before.
     */
    function swapColumnBefore(index, beforeIndex) {
        /* Swap the column info. */
        var colTags = $grid.find("col"),
            tempName,
            table,
            rowsLen,
            i,
            row,
            cell,
            beforeCell,
            tempColSpan;

        $(colTags[index]).insertBefore($(colTags[beforeIndex]));

        /* Swap the list of active columns */
        tempName = activeColumns[index];
        activeColumns[index] = activeColumns[beforeIndex];
        activeColumns[beforeIndex] = tempName;

        /* Swap the cells. This will include the headers. */
        table = $grid.find("table:first")[0];

        for (i = 0, rowsLen = table.rows.length; i < rowsLen; i++) {
            row = table.rows[i];
            cell = row.cells[index];
            beforeCell = row.cells[beforeIndex];

            row.insertBefore(cell, beforeCell);

            /* Switch the colspans. */
            tempColSpan = cell.colSpan;
            cell.colSpan = beforeCell.colSpan;
            beforeCell.colSpan = tempColSpan;
        }

        dragColumnsChanged = true;

        /* Everything has changed, so rebuild our view of things. */
        buildColumnInfo();
    }

    return $grid;
};

$(document).ready(function() {
    $("div.datagrid-wrapper").datagrid();
});

})(jQuery);
